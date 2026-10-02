/**
 * SSE 解析（详细设计 3.4 / 5.1 的 `api/sse.ts`）。
 *
 * 后端帧格式固定为 `event: <name>\ndata: <单行 JSON>\n\n`，因此这里只做最小解析：
 * 逐块拼接缓冲、按空行切帧、忽略注释与非 `event/data` 行。
 *
 * 用 `fetch` + `ReadableStream`（而不是 `EventSource`）：需要 POST 与请求体，
 * 且要能拿到 `AbortController` 的取消能力（5.1 的 `hooks/useChatStream.ts` 依赖它）。
 */

import type { SseEventType } from "@/types/events";

export interface SseFrame {
  event: SseEventType;
  data: Record<string, unknown>;
}

/** 把累积缓冲切成完整帧，返回剩余不完整部分。 */
export function parseSseBuffer(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";

  for (const block of blocks) {
    const frame = parseSseBlock(block);
    if (frame) {
      frames.push(frame);
    }
  }
  return { frames, rest };
}

/** 解析单个 `event:` + `data:` 块；缺 `event` 或 JSON 解析失败返回 `null`（忽略该帧）。 */
export function parseSseBlock(block: string): SseFrame | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) {
      continue; // 注释行（心跳兜底）
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }
  if (!event) {
    return null;
  }
  const payload = dataLines.join("\n");
  try {
    const parsed: unknown = payload ? JSON.parse(payload) : {};
    return { event: event as SseEventType, data: (parsed ?? {}) as Record<string, unknown> };
  } catch {
    return null;
  }
}

/** 消费一个 `text/event-stream` 响应，逐帧产出（`done` 之后立即结束）。 */
export async function* readSse(response: Response): AsyncGenerator<SseFrame> {
  if (!response.body) {
    throw new Error("SSE response has no body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseBuffer(buffer);
      buffer = rest;
      for (const frame of frames) {
        yield frame;
        if (frame.event === "done") {
          return;
        }
      }
    }
    const tail = parseSseBlock(buffer);
    if (tail) {
      yield tail;
    }
  } finally {
    reader.releaseLock();
  }
}
