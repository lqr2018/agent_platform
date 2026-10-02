"""会话 / 消息 / Run 的 DTO（详细设计 3.2.4 / 2.6）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UtcDatetime

ConversationStatus = Literal["active", "archived"]


class ConversationCreate(BaseModel):
    agent_id: str = Field(min_length=26, max_length=26)
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: ConversationStatus | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str = ""
    owner_key: str = "local"
    status: str = "active"
    message_count: int = 0
    last_message_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessageCreate(BaseModel):
    """`POST /conversations/{id}/messages` 的请求体（3.2.4）。

    `stream` 默认 true（前端始终要流）；`model_overrides` 为本阶段的预留位（Phase 1 只接受空 dict）。
    """

    content: str = Field(min_length=1, max_length=32_000)
    stream: bool = True
    model_overrides: dict[str, object] = Field(default_factory=dict)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    run_id: str | None = None
    seq: int
    role: str
    content: str = ""
    name: str | None = None
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    tool_call_id: str | None = None
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    meta: dict[str, object] = Field(default_factory=dict)
    created_at: UtcDatetime


class RunRead(BaseModel):
    """`GET /runs/{run_id}`（3.2.4：含 steps / token / cost）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    agent_id: str | None = None
    conversation_id: str | None = None
    status: str
    input: dict[str, object] = Field(default_factory=dict)
    output: dict[str, object] = Field(default_factory=dict)
    steps: int = 0
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    trace_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    latency_ms: int | None = None
    canceled_at: UtcDatetime | None = None
    created_at: UtcDatetime
