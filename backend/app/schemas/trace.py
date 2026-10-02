"""Trace / Span 的 DTO（详细设计 3.2.9 / 2.11）。

- 列表接口返回**扁平 span**（带 `parent_span_id`），前端自行建树（3.2.9）；
- `GET /spans/{span_id}` 才返回完整 `input` / `output`（列表里不带，避免响应过大）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UtcDatetime


class TraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str | None = None
    workflow_run_id: str | None = None
    name: str = ""
    kind: str = "chat"
    status: str = "running"
    span_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    latency_ms: int | None = None
    created_at: UtcDatetime


class SpanSummary(BaseModel):
    """Trace 详情里的扁平 span（不含 `input` / `output`）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    trace_id: str
    parent_span_id: str | None = None
    run_id: str | None = None
    span_type: str
    name: str = ""
    status: str = "running"
    attributes: dict[str, Any] = Field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    seq: int = 0
    error_code: str | None = None
    error_message: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None


class SpanRead(SpanSummary):
    """`GET /spans/{span_id}`：含完整 `input` / `output`（已按 4.8.2 截断）。"""

    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class TraceDetail(BaseModel):
    """`GET /traces/{trace_id}`：概要 + 扁平 span 列表（3.2.9）。"""

    trace: TraceRead
    spans: list[SpanSummary] = Field(default_factory=list)
