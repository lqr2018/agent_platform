import { beforeEach, describe, expect, it } from "vitest";

import { useChatStore } from "@/stores/chatStore";
import type { SseEventType } from "@/types/events";

/**
 * `chatStore` 的事件归纳（3.4 → UI 状态）。
 *
 * 顺序与后端 Phase 1 实际发出的一致：run.started → message.started → delta* → usage.updated →
 * message.completed → run.completed。
 */
function apply(events: Array<[SseEventType, Record<string, unknown>]>) {
  const { applyEvent } = useChatStore.getState();
  for (const [event, data] of events) {
    applyEvent(event, data);
  }
}

describe("chatStore", () => {
  beforeEach(() => {
    useChatStore.getState().reset();
  });

  it("累积流式增量并记录 Run 元信息", () => {
    apply([
      ["run.started", { run_id: "r1", trace_id: "t1", conversation_id: "c1" }],
      ["message.started", { message_id: "m1", role: "assistant", model: "fake-model" }],
      ["message.delta", { message_id: "m1", delta: "你" }],
      ["message.delta", { message_id: "m1", delta: "好" }],
      ["usage.updated", { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, cost_usd: 0.002 }],
      ["message.completed", { message_id: "m1", finish_reason: "stop", content: "你好" }],
      ["run.completed", { run_id: "r1", status: "succeeded", steps: 1, tool_call_count: 0, latency_ms: 42 }],
    ]);

    const state = useChatStore.getState();
    expect(state.streaming?.content).toBe("你好");
    expect(state.streaming?.finishReason).toBe("stop");
    expect(state.run.status).toBe("succeeded");
    expect(state.run.runId).toBe("r1");
    expect(state.run.traceId).toBe("t1");
    expect(state.run.usage).toEqual({
      promptTokens: 10,
      completionTokens: 5,
      totalTokens: 15,
      costUsd: 0.002,
    });
    expect(state.run.latencyMs).toBe(42);
    expect(state.appliedEvents).toBe(7);
  });

  it("run.failed 记录错误码，RUN_CANCELED 归为取消", () => {
    apply([["run.started", { run_id: "r2", trace_id: "t2" }]]);
    apply([["run.failed", { run_id: "r2", error_code: "MODEL_TIMEOUT", error_message: "timed out" }]]);

    let state = useChatStore.getState();
    expect(state.run.status).toBe("failed");
    expect(state.run.errorCode).toBe("MODEL_TIMEOUT");

    useChatStore.getState().reset();
    apply([
      ["run.started", { run_id: "r3", trace_id: "t3" }],
      ["run.failed", { run_id: "r3", error_code: "RUN_CANCELED", error_message: "canceled" }],
    ]);
    state = useChatStore.getState();
    expect(state.run.status).toBe("canceled");
  });

  it("工具调用时间线在 Phase 2 才有内容，但结构现在就固定", () => {
    apply([
      [
        "tool.call.started",
        { tool_name: "calculator", tool_call_id: "call_1", arguments: { expression: "1+1" } },
      ],
      [
        "tool.call.completed",
        {
          tool_name: "calculator",
          tool_call_id: "call_1",
          status: "succeeded",
          latency_ms: 3,
          result_preview: "2",
        },
      ],
    ]);

    const state = useChatStore.getState();
    expect(state.tools).toHaveLength(1);
    expect(state.tools[0]).toMatchObject({ toolName: "calculator", status: "succeeded", latencyMs: 3 });
  });

  it("retrieval.completed 记录检索骨架，命中时先占位，明细由页面补拉（Phase 5）", () => {
    apply([
      ["run.started", { run_id: "r5", trace_id: "t5" }],
      ["retrieval.completed", { kb_ids: ["kb_1", "kb_2"], query: "怎么安装", hit_count: 2 }],
    ]);

    let state = useChatStore.getState();
    expect(state.citations).toEqual({
      kbIds: ["kb_1", "kb_2"],
      query: "怎么安装",
      hitCount: 2,
      chunks: null,
    });

    useChatStore.getState().setCitationChunks([
      {
        chunkId: "c1",
        documentId: "d1",
        kbId: "kb_1",
        score: 0.83,
        source: "install.md#安装 score=0.83",
        content: "先安装依赖",
      },
    ]);
    state = useChatStore.getState();
    expect(state.citations?.chunks).toHaveLength(1);
    expect(state.citations?.chunks?.[0]?.source).toBe("install.md#安装 score=0.83");
  });

  it("未命中（hit_count = 0）直接落成空明细；新一轮 run.started 清空上一轮引用", () => {
    apply([["retrieval.completed", { kb_ids: ["kb_1"], query: "无关问题", hit_count: 0 }]]);
    expect(useChatStore.getState().citations).toEqual({
      kbIds: ["kb_1"],
      query: "无关问题",
      hitCount: 0,
      chunks: [],
    });

    apply([["run.started", { run_id: "r6", trace_id: "t6" }]]);
    expect(useChatStore.getState().citations).toBeNull();
  });

  it("没触发检索的轮次不渲染引用卡片（citations 保持 null）", () => {
    apply([
      ["run.started", { run_id: "r7", trace_id: "t7" }],
      ["run.completed", { run_id: "r7", status: "succeeded", steps: 1, tool_call_count: 0, latency_ms: 5 }],
    ]);

    expect(useChatStore.getState().citations).toBeNull();
  });

  it("reset 清空状态", () => {
    apply([["message.started", { message_id: "m9", role: "assistant", model: "m" }]]);
    useChatStore.getState().reset();
    expect(useChatStore.getState().streaming).toBeNull();
    expect(useChatStore.getState().run.status).toBe("idle");
  });
});
