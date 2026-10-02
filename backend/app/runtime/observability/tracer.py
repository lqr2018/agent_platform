"""Tracer 骨架（详细设计 4.8.1 / 4.8.2 / 1.5.2）。

Phase 0 只做"日志 + 上下文传播"：span 结束时交给注入的 `SpanSink` 处理，
默认实现 `LoggingSpanSink` 只写日志；Phase 1 起在 `services/` 提供落库 sink（`spans` 表），
`Tracer` 本身**不 import db / services**（1.2 分层）。

Span 树约定（4.8.1）：

    run → agent → {llm, tool, retriever, embedding} / workflow → node

`seq` 在单一 Run 内自增，保证前端排序稳定（4.8.1）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.core import ids
from app.core.config import Settings
from app.core.enums import RunKind, SpanStatus, SpanType
from app.core.errors import AppError
from app.core.logging import get_logger, redact_mapping
from app.runtime.observability import context as trace_context

logger = get_logger(__name__)

_current_run: ContextVar[RunTrace | None] = ContextVar("current_run_trace", default=None)


def utcnow() -> datetime:
    """UTC now（带时区，按 0.2.1）。"""
    return datetime.now(UTC)


@dataclass(slots=True)
class Span:
    """一次 Trace 的最小单位（对应 `spans` 表，Phase 1 起落库，2.11）。"""

    span_id: str
    trace_id: str
    run_id: str
    parent_span_id: str | None
    span_type: SpanType
    name: str
    seq: int
    status: SpanStatus = SpanStatus.RUNNING
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    latency_ms: float | None = None
    input: Any = None
    output: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    error_code: str | None = None
    error_message: str | None = None

    def log_fields(self) -> dict[str, Any]:
        """写日志用的扁平字段（不把大 payload 打进日志）。"""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "span_type": str(self.span_type),
            "name": self.name,
            "seq": self.seq,
            "status": str(self.status),
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error_code": self.error_code,
        }


@dataclass(slots=True)
class RunTrace:
    """一次 Run 的 Trace 句柄（由 `Tracer.start_run` 创建，4.8.2）。"""

    trace_id: str
    run_id: str
    kind: RunKind
    name: str
    root_span: Span
    _seq: int = 1  # 根 span 已占用 seq=1（4.8.1）

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq


class SpanSink(Protocol):
    """span 落地方式（Phase 0 = 日志；Phase 1+ = `spans` 表 / SSE 推送）。"""

    async def write_span(self, span: Span) -> None: ...


class LoggingSpanSink:
    """默认 sink：只写日志（7.1 任务 5）。"""

    async def write_span(self, span: Span) -> None:
        logger.info("trace.span", **span.log_fields())


def _prepare_payload(payload: Any, max_bytes: int) -> Any:
    """截断超大 payload（4.8.2）：超阈值时只留前 N 字节并附 `truncated` 元信息。"""
    if payload is None:
        return None
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return payload
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "preview": encoded[:max_bytes].decode("utf-8", errors="ignore"),
    }


class Tracer:
    """Run / Span 的生命周期与上下文（1.5.2、4.8）。"""

    def __init__(
        self,
        *,
        sink: SpanSink | None = None,
        store_io: bool = True,
        max_payload_bytes: int = 8_192,
    ) -> None:
        self._sink: SpanSink = sink or LoggingSpanSink()
        self._store_io = store_io
        self._max_payload_bytes = max_payload_bytes

    @classmethod
    def from_settings(cls, settings: Settings, *, sink: SpanSink | None = None) -> Tracer:
        return cls(
            sink=sink,
            store_io=settings.trace_store_io,
            max_payload_bytes=settings.trace_max_payload_bytes,
        )

    @property
    def store_io(self) -> bool:
        """是否记录 span 的 `input` / `output`（`TRACE_STORE_IO`）。"""
        return self._store_io

    @property
    def max_payload_bytes(self) -> int:
        """payload 截断阈值（`TRACE_MAX_PAYLOAD_BYTES`，4.8.2）。"""
        return self._max_payload_bytes

    def current_run(self) -> RunTrace | None:
        """当前上下文里的 Run Trace 句柄（Phase 1 起供 `AgentRuntime` 读取，1.5.2）。"""
        return _current_run.get()

    # ---- Run 生命周期 ----
    def start_run(
        self,
        *,
        kind: RunKind,
        name: str,
        run_id: str | None = None,
        trace_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> RunTrace:
        """创建根 span 并把 `trace_id` / `span_id` 写入 ContextVar（1.5.2、4.8.1）。

        `trace_id` 可显式传入（Phase 1 起：服务层先落 `runs.trace_id`，再交给 Tracer 复用同一 id）。
        """
        resolved_trace_id = trace_id or ids.new_trace_id()
        resolved_run_id = run_id or ids.new_ulid()
        root_span = Span(
            span_id=ids.new_span_id(),
            trace_id=resolved_trace_id,
            run_id=resolved_run_id,
            parent_span_id=None,
            span_type=SpanType.RUN,
            name=name,
            seq=1,
            attributes=dict(attributes or {}),
        )
        run_trace = RunTrace(
            trace_id=resolved_trace_id,
            run_id=resolved_run_id,
            kind=kind,
            name=name,
            root_span=root_span,
        )
        _current_run.set(run_trace)
        trace_context.bind_trace(resolved_trace_id, root_span.span_id)
        logger.info(
            "trace.run.started",
            kind=str(kind),
            name=name,
            trace_id=resolved_trace_id,
            run_id=resolved_run_id,
        )
        return run_trace

    async def end_run(
        self,
        run_trace: RunTrace,
        *,
        status: SpanStatus = SpanStatus.OK,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """结束根 span 并清空上下文（Run 的统一出口）。"""
        span = run_trace.root_span
        self._finish(span, status=status, error_code=error_code, error_message=error_message)
        await self._emit(span)
        logger.info(
            "trace.run.finished",
            trace_id=run_trace.trace_id,
            run_id=run_trace.run_id,
            status=str(status),
            latency_ms=span.latency_ms,
            error_code=error_code,
        )
        _current_run.set(None)
        trace_context.clear()

    # ---- Span ----
    @asynccontextmanager
    async def span(
        self,
        span_type: SpanType,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        input: Any = None,  # noqa: A002 - 与 spans.input 字段同名（2.11）
    ) -> AsyncIterator[Span]:
        """`async with tracer.span(...)`：退出时收敛 `status` / `latency_ms` 并交给 sink。

        异常路径：`AppError` 取 `code`，其它异常取异常类名（4.8.2）。
        """
        run_trace = _current_run.get()
        if run_trace is None:
            raise RuntimeError("Tracer.span() requires an active run; call start_run() first")

        span = Span(
            span_id=ids.new_span_id(),
            trace_id=run_trace.trace_id,
            run_id=run_trace.run_id,
            parent_span_id=trace_context.get_span_id(),
            span_type=span_type,
            name=name,
            seq=run_trace.next_seq(),
            attributes=dict(attributes or {}),
            input=_prepare_payload(redact_mapping(input), self._max_payload_bytes) if self._store_io else None,
        )
        span_token = trace_context.bind_span_id(span.span_id)
        try:
            yield span
        except AppError as exc:
            self._finish(span, status=SpanStatus.ERROR, error_code=str(exc.code), error_message=exc.message)
            raise
        except Exception as exc:
            self._finish(span, status=SpanStatus.ERROR, error_code=type(exc).__name__, error_message=str(exc))
            raise
        else:
            self._finish(span, status=SpanStatus.OK)
        finally:
            trace_context.reset_span_id(span_token)
            await self._emit(span)

    # ---- 内部 ----
    def _finish(
        self,
        span: Span,
        *,
        status: SpanStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        span.status = status
        span.ended_at = utcnow()
        span.latency_ms = round((span.ended_at - span.started_at).total_seconds() * 1000, 3)
        span.error_code = error_code
        span.error_message = error_message
        if not self._store_io:
            # 2.11 / 4.8.2：TRACE_STORE_IO=false 时 input/output 一律存 null
            span.input = None
            span.output = None
        else:
            span.input = _prepare_payload(redact_mapping(span.input), self._max_payload_bytes)
            span.output = _prepare_payload(redact_mapping(span.output), self._max_payload_bytes)
        span.attributes = redact_mapping(span.attributes)

    async def _emit(self, span: Span) -> None:
        """写库 / 推送失败不影响主流程（4.8.2）。"""
        try:
            await self._sink.write_span(span)
        except Exception:
            logger.warning("trace.sink_failed", span_id=span.span_id, exc_info=True)
