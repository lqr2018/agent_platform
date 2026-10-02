/**
 * Agent（详细设计 3.2.2 / 5.1）。
 *
 * Phase 1 的 Agent 是"纯对话型"：`tool_ids` / `knowledge_base_ids` / `workflow_id` 必须为空，
 * 因此表单里不出现这三项（SD-14②：未实现的能力不提供入口）。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type AgentRead = components["schemas"]["AgentRead"];
export type AgentCreate = components["schemas"]["AgentCreate"];
export type AgentUpdate = components["schemas"]["AgentUpdate"];
export type AgentCloneRequest = components["schemas"]["AgentCloneRequest"];
export type AgentPromptVersionRead = components["schemas"]["AgentPromptVersionRead"];

export interface AgentListQuery {
  q?: string;
  status?: string;
  tag?: string;
}

export function listAgents(params: AgentListQuery = {}): Promise<AgentRead[]> {
  const query = new URLSearchParams();
  if (params.q) {
    query.set("q", params.q);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.tag) {
    query.set("tag", params.tag);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<AgentRead[]>(`/agents${suffix}`);
}

export function getAgent(id: string): Promise<AgentRead> {
  return apiFetch<AgentRead>(`/agents/${id}`);
}

export function createAgent(payload: AgentCreate): Promise<AgentRead> {
  return apiFetch<AgentRead>("/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateAgent(id: string, payload: AgentUpdate): Promise<AgentRead> {
  return apiFetch<AgentRead>(`/agents/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteAgent(id: string): Promise<void> {
  return apiFetch<void>(`/agents/${id}`, { method: "DELETE" });
}

export function cloneAgent(id: string, payload: AgentCloneRequest): Promise<AgentRead> {
  return apiFetch<AgentRead>(`/agents/${id}/clone`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function listPromptVersions(id: string): Promise<AgentPromptVersionRead[]> {
  return apiFetch<AgentPromptVersionRead[]>(`/agents/${id}/prompt-versions`);
}
