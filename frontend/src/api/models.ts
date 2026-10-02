/**
 * 模型 Provider（详细设计 3.2.2 / 5.1：`api/` 每资源一个文件，仅做请求与类型标注）。
 *
 * 响应里永远只有 `api_key_masked`（1.4 规则 1），请求才带明文 `api_key`。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type ProviderRead = components["schemas"]["ProviderRead"];
export type ProviderCreate = components["schemas"]["ProviderCreate"];
export type ProviderUpdate = components["schemas"]["ProviderUpdate"];
export type ProviderTestResult = components["schemas"]["ProviderTestResult"];
export type ModelEntry = components["schemas"]["ModelEntry"];

/** 列表（`?q=&status=`）。 */
export function listProviders(params: { q?: string; status?: string } = {}): Promise<ProviderRead[]> {
  const query = new URLSearchParams();
  if (params.q) {
    query.set("q", params.q);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<ProviderRead[]>(`/model-providers${suffix}`);
}

export function createProvider(payload: ProviderCreate): Promise<ProviderRead> {
  return apiFetch<ProviderRead>("/model-providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateProvider(id: string, payload: ProviderUpdate): Promise<ProviderRead> {
  return apiFetch<ProviderRead>(`/model-providers/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteProvider(id: string): Promise<void> {
  return apiFetch<void>(`/model-providers/${id}`, { method: "DELETE" });
}

/** 连通性检测（后端会写回 `last_check_at` / `last_check_error`）。 */
export function testProvider(id: string): Promise<ProviderTestResult> {
  return apiFetch<ProviderTestResult>(`/model-providers/${id}/test`, { method: "POST" });
}
