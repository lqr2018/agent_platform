"""SSE 事件模型（详细设计 3.4 表格 + 附录 B；落点见 1.1 的 `core/events.py`）。

- `SseEventType` 的取值集合 **必须** 等于《详细设计》3.4 表的 21 个事件名
  （由 `tests/unit/test_sse_contract.py` 解析文档断言，前端 `types/events.ts` 与之对应）；
- Phase 1 只产生 1/3/4/5/16/17/18/19/20/21 号事件（7.2 任务 6）；其余事件在本模块**已定义名字**，
  payload 模型随对应 Phase 补齐（事件名集合是一次性对齐的整体契约，见 3.4 的显式豁免）；
- `format_sse()` 输出 `event: <name>\\ndata: <json>\\n\\n`，`data` 为单行 JSON
  （3.4：内部不含裸换行）；
- 时间字段复用 `schemas/common.py` 的 `UtcDatetime`（`…Z` 形态，3.1），避免两处格式化逻辑漂移。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime

SseQueueItem = tuple["SseEventType", dict[str, Any]] | None
"""SSE 事件队列的元素：`(事件, payload)`；`None` 是"流结束"哨兵。

`chat_service`（Chat 流）与 `workflow_service`（Chat 内联 Workflow，3.4）共用同一种队列形状，
HTTP 层（`api/v1/chat.py`）只认它 —— 于是两条路径可以复用同一个 SSE 端点。
"""


class SseEventType(StrEnum):
    """3.4 / 附录 B 的 21 个事件名（顺序与表格一致）。"""

    RUN_STARTED = "run.started"
    AGENT_STEP_STARTED = "agent.step.started"
    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    APPROVAL_REQUIRED = "approval.required"  # Backlog（SD-17）
    RETRIEVAL_COMPLETED = "retrieval.completed"
    MEMORY_RECALLED = "memory.recalled"  # Backlog（SD-15）
    MEMORY_UPDATED = "memory.updated"  # Backlog（SD-15）
    WORKFLOW_NODE_STARTED = "workflow.node.started"
    WORKFLOW_NODE_COMPLETED = "workflow.node.completed"
    AGENT_STEP_COMPLETED = "agent.step.completed"
    USAGE_UPDATED = "usage.updated"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    DONE = "done"


# ---- Phase 1 会产生的 payload（字段取自 3.4 表的"关键字段"列 + 3.3.2 的完整示例） ----
class RunStartedPayload(BaseModel):
    run_id: str
    trace_id: str
    conversation_id: str | None = None
    started_at: UtcDatetime


class MessageStartedPayload(BaseModel):
    message_id: str
    role: str = "assistant"
    model: str = ""


class MessageDeltaPayload(BaseModel):
    message_id: str
    delta: str


class MessageCompletedPayload(BaseModel):
    message_id: str
    finish_reason: str
    content: str = ""


class UsageUpdatedPayload(BaseModel):
    """3.4 的 `prompt/completion_tokens` → `prompt_tokens` / `completion_tokens`。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class RunCompletedPayload(BaseModel):
    run_id: str
    status: str
    steps: int = 0
    tool_call_count: int = 0
    latency_ms: int = 0


class RunFailedPayload(BaseModel):
    run_id: str
    error_code: str
    error_message: str = ""


class HeartbeatPayload(BaseModel):
    """3.4：`heartbeat` 不得携带业务字段。"""

    ts: UtcDatetime


class DonePayload(BaseModel):
    """流结束哨兵（3.4 事件 21：`done` 必须是最后发送的事件）。"""

    model_config = {"extra": "forbid"}


# ---- 后续 Phase 会发出的 payload：名字与字段按 3.4 表先固化（实现随阶段接入） ----
class AgentStepStartedPayload(BaseModel):
    """3.4 事件 2（Phase 2）。"""

    step_index: int
    max_steps: int


class AgentStepCompletedPayload(BaseModel):
    """3.4 事件 15（Phase 2）。"""

    step_index: int
    has_tool_calls: bool = False


class ToolCallStartedPayload(BaseModel):
    """3.4 事件 6（Phase 2）。"""

    tool_name: str
    tool_call_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallCompletedPayload(BaseModel):
    """3.4 事件 7（Phase 2）。"""

    tool_name: str
    tool_call_id: str = ""
    status: str = "succeeded"
    latency_ms: int = 0
    result_preview: str = ""


class ToolCallFailedPayload(BaseModel):
    """3.4 事件 8（Phase 2）。"""

    tool_name: str
    tool_call_id: str = ""
    error_code: str
    error_message: str = ""


class RetrievalCompletedPayload(BaseModel):
    """3.4 事件 10（Phase 5）。"""

    kb_ids: list[str] = Field(default_factory=list)
    query: str = ""
    hit_count: int = 0


class WorkflowNodeStartedPayload(BaseModel):
    """3.4 事件 13（Phase 3）。"""

    node_id: str
    node_type: str


class WorkflowNodeCompletedPayload(BaseModel):
    """3.4 事件 14（Phase 3）。"""

    node_id: str
    status: str


class SseErrorBody(BaseModel):
    """3.4 的 `error.code, message` —— 复用 1.6 错误体的 `error` 与 `meta`。"""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SseErrorMeta(BaseModel):
    request_id: str = ""
    trace_id: str | None = None


class ErrorPayload(BaseModel):
    error: SseErrorBody
    meta: SseErrorMeta = Field(default_factory=SseErrorMeta)

    @classmethod
    def of(cls, body: Mapping[str, Any], *, request_id: str = "", trace_id: str | None = None) -> ErrorPayload:
        """由 `AppError.to_body()` 的结果构造（api 层负责补齐 `meta`）。"""
        return cls(
            error=SseErrorBody(
                code=str(body.get("code", "INTERNAL_ERROR")),
                message=str(body.get("message", "Internal error")),
                details=dict(body.get("details") or {}),
            ),
            meta=SseErrorMeta(request_id=request_id, trace_id=trace_id),
        )


def format_sse(event: SseEventType | str, data: BaseModel | Mapping[str, Any] | None = None) -> str:
    """把事件与 payload 编码为一帧 SSE（`data` 单行 JSON，3.4）。"""
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data or {})
    # separators 去掉多余空格；ensure_ascii=False 保留中文（前端按 UTF-8 解析）
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return f"event: {event!s}\ndata: {body}\n\n"


def _json_default(value: Any) -> str:
    """兜底序列化（`datetime` → ISO8601 UTC；其余转字符串）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


SSE_PAYLOAD_MODELS: Mapping[SseEventType, type[BaseModel]] = {
    SseEventType.RUN_STARTED: RunStartedPayload,
    SseEventType.AGENT_STEP_STARTED: AgentStepStartedPayload,
    SseEventType.MESSAGE_STARTED: MessageStartedPayload,
    SseEventType.MESSAGE_DELTA: MessageDeltaPayload,
    SseEventType.MESSAGE_COMPLETED: MessageCompletedPayload,
    SseEventType.TOOL_CALL_STARTED: ToolCallStartedPayload,
    SseEventType.TOOL_CALL_COMPLETED: ToolCallCompletedPayload,
    SseEventType.TOOL_CALL_FAILED: ToolCallFailedPayload,
    SseEventType.RETRIEVAL_COMPLETED: RetrievalCompletedPayload,
    SseEventType.WORKFLOW_NODE_STARTED: WorkflowNodeStartedPayload,
    SseEventType.WORKFLOW_NODE_COMPLETED: WorkflowNodeCompletedPayload,
    SseEventType.AGENT_STEP_COMPLETED: AgentStepCompletedPayload,
    SseEventType.USAGE_UPDATED: UsageUpdatedPayload,
    SseEventType.RUN_COMPLETED: RunCompletedPayload,
    SseEventType.RUN_FAILED: RunFailedPayload,
    SseEventType.ERROR: ErrorPayload,
    SseEventType.HEARTBEAT: HeartbeatPayload,
    SseEventType.DONE: DonePayload,
}
"""事件 → payload 模型（3 个 Backlog 事件没有模型：MVP 不产生它们，SD-14②）。

`tests/unit/test_sse_contract.py` 用它断言"3.4 表列出的关键字段都在模型里"。
"""

SSE_HEADERS: Mapping[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx 反代关闭缓冲（3.4 / docker/nginx.conf）
    "X-Accel-Buffering": "no",
}
