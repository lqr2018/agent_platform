"""`traces` / `spans`（详细设计 2.11）。

- `traces.id` 即 `trace_id`（32 位小写十六进制，0.2.1），`spans.id` 即 `span_id`（16 位）；
- 落库策略：span 结束时**立即写一行**（崩溃后 Trace 仍可用，2.11）；
- 索引：`spans(trace_id, seq)`、`spans(run_id)`、`traces(run_id)`、`traces(started_at)`（2.13）；
- `input` / `output` 由 `Tracer` 截断 + 脱敏后写入（4.8.2），`TRACE_STORE_IO=false` 时为 NULL。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import RunKind, RunStatus, SpanStatus, SpanType
from app.db.base import Base, JSONDict, now_utc


class Trace(Base):
    """一次 Run 的 Trace 概要（2.11）。"""

    __tablename__ = "traces"
    __table_args__ = (Index("ix_traces_run_id", "run_id"), Index("ix_traces_started_at", "started_at"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default=RunKind.CHAT, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING, nullable=False)
    span_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class Span(Base):
    """Trace 最小单位（2.11）：`run → agent → llm/tool/retriever`（4.8.1）。"""

    __tablename__ = "spans"
    __table_args__ = (
        Index("ix_spans_trace_seq", "trace_id", "seq"),
        Index("ix_spans_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    span_type: Mapped[str] = mapped_column(String(16), default=SpanType.RUN, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=SpanStatus.RUNNING, nullable=False)
    # 2.11 / 4.8.2：TRACE_STORE_IO=false 时 input/output 存 NULL
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[JSONDict]
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
