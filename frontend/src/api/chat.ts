/**
 * 一轮对话（详细设计 3.2.4 / 3.4 / 5.1）。
 *
 * `POST /conversations/{id}/messages` 返回 `text/event-stream`，**不是** JSON 信封，
 * 所以这里直接返回 `Response`，由 `hooks/useChatStream.ts` 用 `readSse` 消费。
 */

import type { components } from "@/types/api";

import { API_BASE } from "./client";

export type MessageCreate = components["schemas"]["MessageCreate"];

/** 发起一轮对话，返回 SSE 响应（`signal` 用于离开页面时中断连接）。 */
export async function postMessage(
  conversationId: string,
  payload: MessageCreate,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetch(`${API_BASE}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`);
  }
  return response;
}
