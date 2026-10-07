/**
 * 工具（详细设计 3.2.3 / 5.1）。
 *
 * 七个端点一一对应：列表 / 新建 / 详情 / 更新 / 删除 / 试跑 / 调用明细。
 * 内置工具的 `description` / `input_schema` 等列**以代码为准**（4.2.2 的单一来源），
 * 编辑表单只放开 `status` 与 `permission_config`（2.5："可禁用 / 可改权限"）。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type ToolRead = components["schemas"]["ToolRead"];
export type ToolDetail = components["schemas"]["ToolDetail"];
export type ToolCreate = components["schemas"]["ToolCreate"];
export type ToolUpdate = components["schemas"]["ToolUpdate"];
export type ToolTestResult = components["schemas"]["ToolTestResult"];
export type ToolInvocationRead = components["schemas"]["ToolInvocationRead"];
/** 请求侧的权限配置（与后端的 `ToolPermissionConfigDTO` 同构）。 */
export type ToolPermissionConfig = components["schemas"]["ToolPermissionConfigDTO"];
export type PermissionLevel = NonNullable<ToolPermissionConfig["level"]>;

export interface ToolListQuery {
  tool_type?: string;
  status?: string;
  q?: string;
}

export function listTools(params: ToolListQuery = {}): Promise<ToolRead[]> {
  const query = new URLSearchParams();
  if (params.tool_type) {
    query.set("tool_type", params.tool_type);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.q) {
    query.set("q", params.q);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<ToolRead[]>(`/tools${suffix}`);
}

export function getTool(id: string): Promise<ToolDetail> {
  return apiFetch<ToolDetail>(`/tools/${id}`);
}

export function createTool(payload: ToolCreate): Promise<ToolDetail> {
  return apiFetch<ToolDetail>("/tools", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateTool(id: string, payload: ToolUpdate): Promise<ToolDetail> {
  return apiFetch<ToolDetail>(`/tools/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteTool(id: string): Promise<void> {
  return apiFetch<void>(`/tools/${id}`, { method: "DELETE" });
}

/** 直接执行一次（跳过 LLM）；失败也是 200，看 `ok` / `error_code`。 */
export function testTool(id: string, toolArguments: Record<string, unknown>): Promise<ToolTestResult> {
  return apiFetch<ToolTestResult>(`/tools/${id}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arguments: toolArguments }),
  });
}

export function listToolInvocations(
  params: { run_id?: string; tool_id?: string } = {},
): Promise<ToolInvocationRead[]> {
  const query = new URLSearchParams();
  if (params.run_id) {
    query.set("run_id", params.run_id);
  }
  if (params.tool_id) {
    query.set("tool_id", params.tool_id);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<ToolInvocationRead[]>(`/tool-invocations${suffix}`);
}
