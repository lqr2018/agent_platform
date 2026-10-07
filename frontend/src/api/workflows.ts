/**
 * Workflow（详细设计 3.2.7 / 5.1）。
 *
 * 端点分组（与后端一致）：
 *
 * - 定义：列表 / 新建 / 详情 / 更新 / 删除 / 只校验 / 发布；
 * - 运行：`POST /workflows/{id}/runs`（202）→ 用 `/workflow-runs/{id}/node-runs` 轮询（2s）。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type WorkflowRead = components["schemas"]["WorkflowRead"];
export type WorkflowCreate = components["schemas"]["WorkflowCreate"];
export type WorkflowUpdate = components["schemas"]["WorkflowUpdate"];
export type WorkflowValidationIssue = components["schemas"]["WorkflowValidationIssue"];
export type WorkflowRunRead = components["schemas"]["WorkflowRunRead"];
export type NodeRunRead = components["schemas"]["NodeRunRead"];

/**
 * 只读图投影（后端 `WorkflowGraph.public_definition()`，4.5.1）。
 *
 * 后端 DTO 里 `graph` 是 `dict[str, Any]`（OpenAPI 只知道是对象），前端按这里的强类型消费；
 * 结构由 `tests/unit/workflow/test_workflow_graph.py` 与后端 `public_definition()` 共同保证。
 */
export interface WorkflowGraphNode {
  id: string;
  type: string;
  name: string;
  next: string | null;
  branches: { when: string; next: string }[];
  default_next: string | null;
}

export interface WorkflowGraphProjection {
  start_node_id: string;
  end_node_ids: string[];
  config: Record<string, unknown>;
  nodes: WorkflowGraphNode[];
}

export type GraphNode = WorkflowGraphNode;

/** `/validate` 的响应：把 `graph` / `errors` 收窄成前端可用的形态（3.2.7）。 */
export type WorkflowValidateResult = Omit<
  components["schemas"]["WorkflowValidateResult"],
  "graph" | "errors"
> & {
  errors: WorkflowValidationIssue[];
  graph: WorkflowGraphProjection | null;
};

export interface WorkflowListQuery {
  status?: string;
  q?: string;
}

export function listWorkflows(params: WorkflowListQuery = {}): Promise<WorkflowRead[]> {
  const query = new URLSearchParams();
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.q) {
    query.set("q", params.q);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<WorkflowRead[]>(`/workflows${suffix}`);
}

export function getWorkflow(id: string): Promise<WorkflowRead> {
  return apiFetch<WorkflowRead>(`/workflows/${id}`);
}

export function createWorkflow(payload: WorkflowCreate): Promise<WorkflowRead> {
  return apiFetch<WorkflowRead>("/workflows", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateWorkflow(id: string, payload: WorkflowUpdate): Promise<WorkflowRead> {
  return apiFetch<WorkflowRead>(`/workflows/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteWorkflow(id: string): Promise<void> {
  return apiFetch<void>(`/workflows/${id}`, { method: "DELETE" });
}

/**
 * 只校验不落库（3.2.7）。
 *
 * `definition` 省略时校验**已保存**的定义；传了则校验编辑器里的草稿。
 * 不合法也是 200（`valid=false` + `errors`），这样编辑器可以直接渲染错误清单。
 */
export function validateWorkflow(
  id: string,
  definition?: Record<string, unknown>,
): Promise<WorkflowValidateResult> {
  return apiFetch<WorkflowValidateResult>(`/workflows/${id}/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition ? { definition } : {}),
  });
}

export function publishWorkflow(id: string): Promise<WorkflowRead> {
  return apiFetch<WorkflowRead>(`/workflows/${id}/publish`, { method: "POST" });
}

/** 启动运行：返回 202 的 `workflow_runs`（进度靠 `listNodeRuns` 轮询，3.4）。 */
export function startWorkflowRun(id: string, input: Record<string, unknown>): Promise<WorkflowRunRead> {
  return apiFetch<WorkflowRunRead>(`/workflows/${id}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });
}

export function listWorkflowRuns(
  params: { workflow_id?: string; status?: string } = {},
): Promise<WorkflowRunRead[]> {
  const query = new URLSearchParams();
  if (params.workflow_id) {
    query.set("workflow_id", params.workflow_id);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<WorkflowRunRead[]>(`/workflow-runs${suffix}`);
}

export function getWorkflowRun(runId: string): Promise<WorkflowRunRead> {
  return apiFetch<WorkflowRunRead>(`/workflow-runs/${runId}`);
}

export function listNodeRuns(runId: string): Promise<NodeRunRead[]> {
  return apiFetch<NodeRunRead[]>(`/workflow-runs/${runId}/node-runs`);
}

export function resumeWorkflowRun(runId: string): Promise<WorkflowRunRead> {
  return apiFetch<WorkflowRunRead>(`/workflow-runs/${runId}/resume`, { method: "POST" });
}

export function cancelWorkflowRun(runId: string): Promise<WorkflowRunRead> {
  return apiFetch<WorkflowRunRead>(`/workflow-runs/${runId}/cancel`, { method: "POST" });
}
