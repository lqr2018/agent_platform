/**
 * SSE 事件契约（详细设计 3.4 表 + 附录 B；后端 `app/core/events.py::SseEventType` 的镜像）。
 *
 * 三处必须一致：本文档的联合类型字面量 == 后端枚举取值 == 《详细设计》3.4 表的 21 个事件名
 * （由 `backend/tests/unit/test_sse_contract.py` 断言）。新增事件请同时改这三处 + 3.4 表 + 附录 B。
 *
 * 标 `Backlog` 的事件（`approval.required` / `memory.recalled` / `memory.updated`）在 MVP
 * **不会收到**，保留在联合类型里是为了迭代 A/C 时不破坏前端类型契约（3.4 的显式豁免）。
 */
export type SseEventType =
  | "run.started"
  | "agent.step.started"
  | "message.started"
  | "message.delta"
  | "message.completed"
  | "tool.call.started"
  | "tool.call.completed"
  | "tool.call.failed"
  | "approval.required"
  | "retrieval.completed"
  | "memory.recalled"
  | "memory.updated"
  | "workflow.node.started"
  | "workflow.node.completed"
  | "agent.step.completed"
  | "usage.updated"
  | "run.completed"
  | "run.failed"
  | "error"
  | "heartbeat"
  | "done";

/** `run.started`（3.4 事件 1）。 */
export interface RunStartedPayload {
  run_id: string;
  trace_id: string;
  conversation_id?: string | null;
  started_at: string;
}

/** `agent.step.started`（事件 2，Phase 2）。 */
export interface AgentStepStartedPayload {
  step_index: number;
  max_steps: number;
}

/** `message.started`（事件 3）。 */
export interface MessageStartedPayload {
  message_id: string;
  role: string;
  model: string;
}

/** `message.delta`（事件 4）。 */
export interface MessageDeltaPayload {
  message_id: string;
  delta: string;
}

/** `message.completed`（事件 5）。 */
export interface MessageCompletedPayload {
  message_id: string;
  finish_reason: string;
  content: string;
}

/** `tool.call.started`（事件 6，Phase 2）。 */
export interface ToolCallStartedPayload {
  tool_name: string;
  tool_call_id: string;
  arguments: Record<string, unknown>;
}

/** `tool.call.completed`（事件 7，Phase 2）。 */
export interface ToolCallCompletedPayload {
  tool_name: string;
  tool_call_id: string;
  status: string;
  latency_ms: number;
  result_preview: string;
}

/** `tool.call.failed`（事件 8，Phase 2）。 */
export interface ToolCallFailedPayload {
  tool_name: string;
  tool_call_id: string;
  error_code: string;
  error_message: string;
}

/** `retrieval.completed`（事件 10，Phase 5）。 */
export interface RetrievalCompletedPayload {
  kb_ids: string[];
  query: string;
  hit_count: number;
}

/** `workflow.node.started`（事件 13，Phase 3）。 */
export interface WorkflowNodeStartedPayload {
  node_id: string;
  node_type: string;
}

/** `workflow.node.completed`（事件 14，Phase 3）。 */
export interface WorkflowNodeCompletedPayload {
  node_id: string;
  status: string;
}

/** `agent.step.completed`（事件 15，Phase 2）。 */
export interface AgentStepCompletedPayload {
  step_index: number;
  has_tool_calls: boolean;
}

/** `usage.updated`（事件 16）。 */
export interface UsageUpdatedPayload {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

/** `run.completed`（事件 17）。 */
export interface RunCompletedPayload {
  run_id: string;
  status: string;
  steps: number;
  tool_call_count: number;
  latency_ms: number;
}

/** `run.failed`（事件 18）。 */
export interface RunFailedPayload {
  run_id: string;
  error_code: string;
  error_message: string;
}

/** `error`（事件 19）：沿用 1.6 的错误信封。 */
export interface SseErrorPayload {
  error: { code: string; message: string; details: Record<string, unknown> };
  meta: { request_id: string; trace_id?: string | null };
}

/** `heartbeat`（事件 20）：不带业务字段。 */
export interface HeartbeatPayload {
  ts: string;
}

/** `done`（事件 21）：流结束哨兵。 */
export type DonePayload = Record<string, never>;

/** 事件名 → payload 类型（`useChatStream` 按此收窄）。 */
export interface SseEventPayloadMap {
  "run.started": RunStartedPayload;
  "agent.step.started": AgentStepStartedPayload;
  "message.started": MessageStartedPayload;
  "message.delta": MessageDeltaPayload;
  "message.completed": MessageCompletedPayload;
  "tool.call.started": ToolCallStartedPayload;
  "tool.call.completed": ToolCallCompletedPayload;
  "tool.call.failed": ToolCallFailedPayload;
  "approval.required": Record<string, unknown>;
  "retrieval.completed": RetrievalCompletedPayload;
  "memory.recalled": Record<string, unknown>;
  "memory.updated": Record<string, unknown>;
  "workflow.node.started": WorkflowNodeStartedPayload;
  "workflow.node.completed": WorkflowNodeCompletedPayload;
  "agent.step.completed": AgentStepCompletedPayload;
  "usage.updated": UsageUpdatedPayload;
  "run.completed": RunCompletedPayload;
  "run.failed": RunFailedPayload;
  error: SseErrorPayload;
  heartbeat: HeartbeatPayload;
  done: DonePayload;
}

/** 解析后的一帧（`api/sse.ts` 的产出）。 */
export interface SseFrame {
  event: SseEventType;
  data: Record<string, unknown>;
}

export const SSE_EVENT_TYPES: readonly SseEventType[] = [
  "run.started",
  "agent.step.started",
  "message.started",
  "message.delta",
  "message.completed",
  "tool.call.started",
  "tool.call.completed",
  "tool.call.failed",
  "approval.required",
  "retrieval.completed",
  "memory.recalled",
  "memory.updated",
  "workflow.node.started",
  "workflow.node.completed",
  "agent.step.completed",
  "usage.updated",
  "run.completed",
  "run.failed",
  "error",
  "heartbeat",
  "done",
];
