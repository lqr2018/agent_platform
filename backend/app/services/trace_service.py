"""Trace 落库与查询（详细设计 4.8 / 2.11 / 3.2.9）。

- `DatabaseSpanSink`：`Tracer` 的落库 sink —— span 结束时**立即写一行**（2.11 的落库策略）；
  写库失败只记 warning，不影响主流程（4.8.2，由 `Tracer` 兜底）；
- `traces` 行在 Run 开始时插入（`open_trace`），Run 结束时更新终态与汇总（4.8.3）；
- 查询接口供 `GET /traces*` / `GET /spans/{id}` 使用：列表返回扁平 span（3.2.9）。
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus, SpanType
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models import Span, Trace
from app.runtime.observability.tracer import RunTrace
from app.runtime.observability.tracer import Span as RuntimeSpan

logger = get_logger(__name__)

TRACE_PAGE_LIMIT = 50
TRACE_PAGE_MAX = 200


@dataclass(slots=True)
class _TraceTotals:
    """`traces` 汇总的进程内累加器（4.8.3：span 结束即累加，Run 结束时写回）。

    `total_tokens` / `total_cost_usd` 只累加 **`llm` span**（`agent` / `run` span 上挂的是聚合值，
    重复累加会翻倍）；`span_count` 统计全部 span（含根 span）。
    """

    span_count: int = 0
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal(0)


class DatabaseSpanSink:
    """把 span / trace 写进 SQLite（4.8.2）。

    与业务写入共用同一个 session：Phase 1 的执行是单任务串行的，共用 session 既避免
    SQLite 写锁竞争，也让"消息 + span"落在同一连接上，行为最可预测。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._totals: dict[str, _TraceTotals] = {}

    async def open_trace(self, run_trace: RunTrace) -> None:
        """Run 开始时插入 `traces` 行（`status=running`）。"""
        trace = Trace(
            id=run_trace.trace_id,
            run_id=run_trace.run_id,
            name=run_trace.name,
            kind=str(run_trace.kind),
            status=str(RunStatus.RUNNING),
            span_count=1,  # 根 span
            started_at=_naive(run_trace.root_span.started_at),
        )
        self._session.add(trace)
        await self._session.commit()
        self._totals[run_trace.trace_id] = _TraceTotals()

    async def write_span(self, span: RuntimeSpan) -> None:
        """span 结束即落库（2.11 的落库策略）。"""
        row = Span(
            id=span.span_id,
            trace_id=span.trace_id,
            parent_span_id=span.parent_span_id,
            run_id=span.run_id,
            span_type=str(span.span_type),
            name=span.name,
            status=str(span.status),
            input=span.input,
            output=span.output,
            attributes=dict(span.attributes or {}),
            prompt_tokens=span.prompt_tokens,
            completion_tokens=span.completion_tokens,
            cost_usd=span.cost_usd,
            latency_ms=int(span.latency_ms) if span.latency_ms is not None else None,
            seq=span.seq,
            error_code=span.error_code,
            error_message=span.error_message,
            started_at=_naive(span.started_at),
            ended_at=_naive(span.ended_at) if span.ended_at else None,
        )
        self._session.add(row)
        totals = self._totals.setdefault(span.trace_id, _TraceTotals())
        totals.span_count += 1
        if str(span.span_type) == str(SpanType.LLM):  # 只在 llm span 上累加用量（见 _TraceTotals 说明）
            totals.total_tokens += span.prompt_tokens + span.completion_tokens
            totals.total_cost_usd += span.cost_usd
        await self._session.commit()

    async def close_trace(
        self,
        run_trace: RunTrace,
        *,
        status: RunStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Run 结束时把汇总写回 `traces`（4.8.3）。"""
        trace = await self._session.get(Trace, run_trace.trace_id)
        if trace is None:  # pragma: no cover - open_trace 已插入
            return
        totals = self._totals.get(run_trace.trace_id, _TraceTotals())
        trace.status = str(status)
        trace.ended_at = _naive(run_trace.root_span.ended_at or datetime.now(UTC))
        trace.latency_ms = int(run_trace.root_span.latency_ms or 0)
        trace.span_count = totals.span_count
        trace.total_tokens = totals.total_tokens
        trace.total_cost_usd = totals.total_cost_usd
        trace.error_code = error_code
        trace.error_message = error_message
        await self._session.commit()


# ---- 查询（3.2.9） ----
async def list_traces(
    session: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = TRACE_PAGE_LIMIT,
    cursor: str | None = None,
) -> tuple[Sequence[Trace], str | None]:
    """Trace 列表（游标分页，倒序按 `started_at`）。

    `cursor` 形如 `"{started_at 微秒}|{trace_id}"`：键集分页，避免 offset 在并发写入下漂移。
    """
    page_limit = max(1, min(limit, TRACE_PAGE_MAX))
    statement = select(Trace).order_by(Trace.started_at.desc(), Trace.id.desc()).limit(page_limit + 1)
    if kind:
        statement = statement.where(Trace.kind == kind)
    if status:
        statement = statement.where(Trace.status == status)
    if from_at is not None:
        statement = statement.where(Trace.started_at >= _naive(from_at))
    if to_at is not None:
        statement = statement.where(Trace.started_at <= _naive(to_at))
    if cursor:
        cursor_at, cursor_id = decode_cursor(cursor)
        statement = statement.where(
            or_(Trace.started_at < cursor_at, and_(Trace.started_at == cursor_at, Trace.id < cursor_id))
        )
    rows = list((await session.execute(statement)).scalars().all())
    next_cursor = None
    if len(rows) > page_limit:
        rows = rows[:page_limit]
        next_cursor = encode_cursor(rows[-1])
    return rows, next_cursor


async def get_trace(session: AsyncSession, trace_id: str) -> Trace:
    """取 Trace；不存在 → `NOT_FOUND`（404）。"""
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise NotFoundError(f"Trace '{trace_id}' does not exist", details={"trace_id": trace_id})
    return trace


async def list_spans(session: AsyncSession, trace_id: str) -> Sequence[Span]:
    """同一 trace 内的扁平 span，按 `seq` 排序（3.2.9：前端自行建树）。"""
    statement = select(Span).where(Span.trace_id == trace_id).order_by(Span.seq)
    return (await session.execute(statement)).scalars().all()


async def get_span(session: AsyncSession, span_id: str) -> Span:
    """取单个 span（含完整 `input` / `output`，3.2.9）。"""
    span = await session.get(Span, span_id)
    if span is None:
        raise NotFoundError(f"Span '{span_id}' does not exist", details={"span_id": span_id})
    return span


def encode_cursor(trace: Trace) -> str:
    """Trace → 键集游标（UTC 微秒时间戳 + id）。

    注意：库里存的是 **naive UTC**（0.2.1），所以用 `calendar.timegm` 按 UTC 解释，
    不能直接用 `datetime.timestamp()`（那会按本机时区解释，导致游标偏移）。
    """
    started_at = _naive(trace.started_at)
    micros = calendar.timegm(started_at.timetuple()) * 1_000_000 + started_at.microsecond
    return f"{micros}|{trace.id}"


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """游标 → `(started_at, trace_id)`；格式非法时由调用方（路由层）返回 422。"""
    micros_text, _, trace_id = cursor.partition("|")
    micros = int(micros_text)
    seconds, remainder = divmod(micros, 1_000_000)
    stamp = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=remainder)
    return stamp.replace(tzinfo=None), trace_id


def _naive(value: datetime) -> datetime:
    """aware → naive UTC（SQLite 存 naive，0.2.1）。"""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
