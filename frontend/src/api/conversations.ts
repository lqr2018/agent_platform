/**
 * 会话 / 消息 / Run（详细设计 3.2.4 / 5.1）。
 *
 * 消息与 Trace 用游标分页（3.1）：`meta.next_cursor` 原样回传 `?cursor=`。
 * `apiFetch` 只解包 `data`，游标分页需要 `meta`，故这里用 `apiFetchEnvelope`。
 */

import type { components } from "@/types/api";

import { type ApiEnvelope, apiFetch, apiFetchEnvelope } from "./client";

export type ConversationRead = components["schemas"]["ConversationRead"];
export type ConversationCreate = components["schemas"]["ConversationCreate"];
export type ConversationUpdate = components["schemas"]["ConversationUpdate"];
export type MessageRead = components["schemas"]["MessageRead"];
export type RunRead = components["schemas"]["RunRead"];

export const MESSAGE_PAGE_SIZE = 50;

export function listConversations(
  params: { agentId?: string; q?: string; page?: number; pageSize?: number } = {},
): Promise<ApiEnvelope<ConversationRead[]>> {
  const query = new URLSearchParams();
  if (params.agentId) {
    query.set("agent_id", params.agentId);
  }
  if (params.q) {
    query.set("q", params.q);
  }
  query.set("page", String(params.page ?? 1));
  query.set("page_size", String(params.pageSize ?? 20));
  return apiFetchEnvelope<ConversationRead[]>(`/conversations?${query.toString()}`);
}

export function createConversation(payload: ConversationCreate): Promise<ConversationRead> {
  return apiFetch<ConversationRead>("/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getConversation(id: string): Promise<ConversationRead> {
  return apiFetch<ConversationRead>(`/conversations/${id}`);
}

export function updateConversation(id: string, payload: ConversationUpdate): Promise<ConversationRead> {
  return apiFetch<ConversationRead>(`/conversations/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteConversation(id: string): Promise<void> {
  return apiFetch<void>(`/conversations/${id}`, { method: "DELETE" });
}

/**
 * 消息列表（倒序：最新在前）；游标从 `meta.next_cursor` 取（3.1 的游标分页）。
 */
export function listMessages(
  conversationId: string,
  params: { limit?: number; cursor?: string } = {},
): Promise<ApiEnvelope<MessageRead[]>> {
  const query = new URLSearchParams();
  query.set("limit", String(params.limit ?? MESSAGE_PAGE_SIZE));
  if (params.cursor) {
    query.set("cursor", params.cursor);
  }
  return apiFetchEnvelope<MessageRead[]>(`/conversations/${conversationId}/messages?${query.toString()}`);
}

export function getRun(runId: string): Promise<RunRead> {
  return apiFetch<RunRead>(`/runs/${runId}`);
}

export function listRuns(
  params: { agentId?: string; conversationId?: string; kind?: string; status?: string; page?: number } = {},
): Promise<ApiEnvelope<RunRead[]>> {
  const query = new URLSearchParams();
  if (params.agentId) {
    query.set("agent_id", params.agentId);
  }
  if (params.conversationId) {
    query.set("conversation_id", params.conversationId);
  }
  if (params.kind) {
    query.set("kind", params.kind);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  query.set("page", String(params.page ?? 1));
  return apiFetchEnvelope<RunRead[]>(`/runs?${query.toString()}`);
}

/** 取消运行中的 Run（3.2.4）；已进入终态会拿到 `RUN_ALREADY_FINISHED`。 */
export function cancelRun(runId: string): Promise<RunRead> {
  return apiFetch<RunRead>(`/runs/${runId}/cancel`, { method: "POST" });
}
