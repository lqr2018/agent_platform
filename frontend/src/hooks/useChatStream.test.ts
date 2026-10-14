import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useChatStream } from "@/hooks/useChatStream";
import type { ChatStreamResult } from "@/hooks/useChatStream";
import { useChatStore } from "@/stores/chatStore";

/**
 * `useChatStream` 的事件分发（7.9「测试要求」点名；3.4 的事件名 → `chatStore` 归纳）。
 *
 * 与 `api/sse.test.ts`（帧解析）/ `stores/chatStore.test.ts`（事件归纳）分工不同：这里钉的是
 * **中间那层** —— 帧按事件名分发、`message.completed` 的 `content` 覆盖增量累积、`run.failed`
 * 与传输层错误各自的收尾，以及 `send()` 的并发闸门与 `stop()` 的断连。
 *
 * 网络一律 stub（`src/test/setup.ts` 的口径）：`fetch` 返回由字符串帧拼成的
 * `text/event-stream`，与后端 `format_sse()` 的输出同形（`event: <name>\ndata: <单行 JSON>\n\n`）。
 */
function frame(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** 一条一次性吐完的 SSE 响应。 */
function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frames.join("")));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const OK_FRAMES = [
  frame("run.started", { run_id: "r1", trace_id: "t1", conversation_id: "c1" }),
  frame("message.started", { message_id: "m1", role: "assistant", model: "fake-model" }),
  frame("message.delta", { message_id: "m1", delta: "你" }),
  frame("message.delta", { message_id: "m1", delta: "好" }),
  frame("usage.updated", { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, cost_usd: 0 }),
  frame("message.completed", { message_id: "m1", finish_reason: "stop", content: "你好" }),
  frame("run.completed", {
    run_id: "r1",
    status: "succeeded",
    steps: 1,
    tool_call_count: 0,
    latency_ms: 42,
  }),
  frame("done", {}),
];

describe("useChatStream", () => {
  beforeEach(() => {
    useChatStore.getState().reset();
  });

  it("逐帧分发给 chatStore（先 reset 清掉上一轮），并把最终回答交给 onSettled", async () => {
    // 显式记下实参：`vi.fn(async () => …)` 的 `mock.calls` 推不出 `[url, init]` 元组
    const calls: Array<[string, RequestInit | undefined]> = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return sseResponse(OK_FRAMES);
    });
    vi.stubGlobal("fetch", fetchMock);

    // 上一轮残留：reset 必须把它清掉，否则 appliedEvents 会是 9 而不是 8
    useChatStore.getState().applyEvent("message.delta", { message_id: "old", delta: "旧内容" });

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    await act(async () => {
      await result.current.send("你好");
    });

    const request = calls[0];
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(request?.[0]).toBe("/api/v1/conversations/c1/messages");
    expect(request?.[1]?.method).toBe("POST");
    expect(request?.[1]?.headers).toMatchObject({ Accept: "text/event-stream" });
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ content: "你好", stream: true });

    const state = useChatStore.getState();
    expect(state.appliedEvents).toBe(8); // reset 生效 + 8 帧全进 store（`done` 也计数；没 reset 会是 9）
    expect(state.streaming?.messageId).toBe("m1");
    expect(state.streaming?.content).toBe("你好");
    expect(state.run.status).toBe("succeeded");
    expect(state.run.latencyMs).toBe(42);
    expect(state.run.usage.totalTokens).toBe(15);

    expect(settled).toEqual([
      { content: "你好", messageId: "m1", finishReason: "stop", ok: true, errorCode: null },
    ]);
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("message.completed 的 content 是权威值：覆盖 delta 累积的中间态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          frame("run.started", { run_id: "r2", trace_id: "t2" }),
          frame("message.started", { message_id: "m2", role: "assistant", model: "fake-model" }),
          frame("message.delta", { message_id: "m2", delta: "你" }),
          frame("message.delta", { message_id: "m2", delta: "好" }),
          frame("message.completed", {
            message_id: "m2",
            finish_reason: "stop",
            content: "你好，我是 Data Analysis Agent。",
          }),
        ]),
      ),
    );

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    await act(async () => {
      await result.current.send("你好");
    });

    expect(useChatStore.getState().streaming?.content).toBe("你好，我是 Data Analysis Agent。");
    expect(settled[0]?.content).toBe("你好，我是 Data Analysis Agent。");
  });

  it("run.failed：ok=false 且带 error_code，Run 记为 failed（流内失败不算传输错误）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          frame("run.started", { run_id: "r3", trace_id: "t3" }),
          frame("run.failed", {
            run_id: "r3",
            error_code: "MODEL_TIMEOUT",
            error_message: "timed out",
          }),
        ]),
      ),
    );

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    await act(async () => {
      await result.current.send("你好");
    });

    expect(settled[0]).toMatchObject({ ok: false, errorCode: "MODEL_TIMEOUT" });
    expect(useChatStore.getState().run.status).toBe("failed");
    expect(useChatStore.getState().run.errorCode).toBe("MODEL_TIMEOUT");
    // 失败是 Run 的结论（页面读 store），不是在流上抛错
    expect(result.current.error).toBeNull();
    expect(result.current.isStreaming).toBe(false);
  });

  it("HTTP 非 2xx：错误文案进 error、onSettled 仍然回调（ok=false），不留 loading", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500, headers: { "Content-Type": "text/plain" } })),
    );

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    await act(async () => {
      await result.current.send("你好");
    });

    expect(result.current.error).toBe("HTTP 500: boom");
    expect(result.current.isStreaming).toBe(false);
    expect(settled[0]).toMatchObject({ ok: false, errorCode: "Error", content: "" });
    expect(useChatStore.getState().appliedEvents).toBe(0);
  });

  it("网络异常（TypeError）：错误码取 Error.name，不把异常抛给调用方", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    await act(async () => {
      await result.current.send("你好");
    });

    expect(result.current.error).toBe("Failed to fetch");
    expect(result.current.isStreaming).toBe(false);
    expect(settled[0]).toMatchObject({ ok: false, errorCode: "TypeError" });
  });

  it("没有会话 id 时 send 直接返回，不打网络", async () => {
    const fetchMock = vi.fn(async () => sseResponse(OK_FRAMES));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useChatStream({ conversationId: undefined }));

    await act(async () => {
      await result.current.send("你好");
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.isStreaming).toBe(false);
  });

  it("同一时刻只允许一个流：未结束时第二次 send 被忽略（后端 409 的客户端侧前置）", async () => {
    const encoder = new TextEncoder();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    // 首帧之后悬住不关：模拟"还在流式生成"
    const stream = new ReadableStream<Uint8Array>({
      async start(controller) {
        controller.enqueue(encoder.encode(frame("run.started", { run_id: "r4", trace_id: "t4" })));
        await gate;
        controller.enqueue(encoder.encode(frame("done", {})));
        controller.close();
      },
    });
    const fetchMock = vi.fn(async () => new Response(stream, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useChatStream({ conversationId: "c1" }));

    let first: Promise<void> | undefined;
    await act(async () => {
      first = result.current.send("第一条");
    });
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.send("第二条");
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    release();
    await act(async () => {
      await first;
    });
    expect(result.current.isStreaming).toBe(false);
  });

  it("stop() 断开连接：signal 被 abort、isStreaming 复位、onSettled 收到 ok=false 且不写 error 文案", async () => {
    let capturedSignal: AbortSignal | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        capturedSignal = init?.signal ?? null;
        // 真 fetch 在 abort 时以 AbortError 拒绝；stub 复刻这一行为，避免留下悬空的 await
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError")),
          );
        });
      }),
    );

    const settled: ChatStreamResult[] = [];
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onSettled: (value) => settled.push(value) }),
    );

    let pending: Promise<void> | undefined;
    await act(async () => {
      pending = result.current.send("长回答");
    });
    expect(result.current.isStreaming).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(capturedSignal).not.toBeNull();
    expect((capturedSignal as unknown as AbortSignal).aborted).toBe(true);
    expect(result.current.isStreaming).toBe(false);

    await act(async () => {
      await pending;
    });
    // 3.4：停止只断连、不取消 Run —— 以本地码 `ABORTED` 收尾，且**不写 error 文案**
    // （否则 ChatPage 会把 "The operation was aborted." 当失败提示红色显示）
    expect(settled[0]).toMatchObject({ ok: false, errorCode: "ABORTED" });
    expect(result.current.error).toBeNull();
  });
});
