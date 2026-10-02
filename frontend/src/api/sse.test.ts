import { describe, expect, it } from "vitest";

import { parseSseBlock, parseSseBuffer, readSse } from "@/api/sse";

/**
 * SSE 解析（3.4 的帧格式：`event: <name>\ndata: <单行 JSON>\n\n`）。
 *
 * 这些用例是前后端协议的可执行契约：后端 `format_sse()` 的输出必须能被这里解析。
 */
describe("parseSseBuffer", () => {
  it("解析完整帧并保留不完整尾巴", () => {
    const buffer = 'event: message.delta\ndata: {"delta":"你"}\n\nevent: message.delta\ndata: {"delta":"好"';
    const { frames, rest } = parseSseBuffer(buffer);

    expect(frames).toHaveLength(1);
    expect(frames[0]).toEqual({ event: "message.delta", data: { delta: "你" } });
    expect(rest).toBe('event: message.delta\ndata: {"delta":"好"');
  });

  it("兼容 CRLF 与心跳注释行", () => {
    const buffer = ': keep-alive\r\nevent: heartbeat\r\ndata: {"ts":"2026-10-01T00:00:00.000Z"}\r\n\r\n';
    const { frames, rest } = parseSseBuffer(buffer);

    expect(frames).toHaveLength(1);
    expect(frames[0]?.event).toBe("heartbeat");
    expect(rest).toBe("");
  });

  it("忽略无法解析的帧（坏 JSON / 缺少 event）", () => {
    expect(parseSseBlock("data: {}")).toBeNull();
    expect(parseSseBlock("event: message.delta\ndata: {oops}")).toBeNull();
  });

  it("done 帧的 data 为空对象", () => {
    expect(parseSseBlock("event: done\ndata: {}")).toEqual({ event: "done", data: {} });
  });
});

describe("readSse", () => {
  function streamResponse(chunks: string[]): Response {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    });
    return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  it("跨块拼帧并在 done 之后停止", async () => {
    const response = streamResponse([
      'event: run.started\ndata: {"run_id":"r1"}\n\nevent: message.st',
      'arted\ndata: {"message_id":"m1"}\n\nevent: done\ndata: {}\n\nevent: message.delta\ndata: {"delta":"x"}\n\n',
    ]);

    const collected: string[] = [];
    for await (const frame of readSse(response)) {
      collected.push(frame.event);
    }
    expect(collected).toEqual(["run.started", "message.started", "done"]);
  });
});
