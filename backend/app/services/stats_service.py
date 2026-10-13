"""平台指标聚合（详细设计 3.2.9 / 4.8.3 / 7.8 任务 5）。

- `GET /stats/overview` 直接聚合 `runs` / `spans`（4.8.3：SQLite 下用 SQL 聚合，不引入时序库）；
- 字段名与 4.9.2 的报告口径**共用一套**（`success_rate` / `avg_latency_ms` / `avg_tool_calls` /
  `avg_total_tokens` / `total_cost_usd` / `error_rate` / `tool_success_rate`）；
- **公式写死在这里**（不让前端猜）：
  - 窗口按 `runs.started_at`（tool span 按 `traces.started_at`）闭区间过滤，`from` / `to` 与
    `GET /traces` 同口径（`db/base.to_naive_utc`）；
  - `terminal_runs = succeeded + failed + canceled` —— `pending` / `running` 还没收敛，**不进分母**；
  - `success_rate = succeeded / terminal_runs`、`error_rate = failed / terminal_runs`
    （"取消"不算错误，对齐 4.9.2 的 `error` 只指执行异常）；分母为 0 → `null`；
  - 三个均值只统计**收敛**的 Run：`tool_call_count` / `total_tokens` 的列默认值是 `0`（不是 NULL），
    把在跑的 Run 算进来会系统性拉低均值，所以单独用一条 `status IN (终态)` 的查询；
  - `tool_success_rate = status=ok 的 tool span / 全部 tool span`（无 tool span → `null`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.enums import RunStatus, SpanStatus, SpanType
from app.db.base import to_naive_utc
from app.db.models import Run, Span, Trace

_TERMINAL: tuple[str, ...] = (str(RunStatus.SUCCEEDED), str(RunStatus.FAILED), str(RunStatus.CANCELED))
_UNSETTLED: tuple[str, ...] = (str(RunStatus.PENDING), str(RunStatus.RUNNING))


@dataclass(slots=True)
class StatsTotals:
    """聚合结果（字段名与 `schemas/stats.py::StatsOverview` 一一对应，路由层直接 `model_validate`）。"""

    generated_at: datetime
    from_at: datetime | None
    to_at: datetime | None
    kind: str | None
    total_runs: int
    succeeded: int
    failed: int
    canceled: int
    running: int
    terminal_runs: int
    success_rate: float | None
    error_rate: float | None
    avg_latency_ms: float | None
    avg_tool_calls: float | None
    avg_total_tokens: float | None
    total_tokens: int
    total_cost_usd: Decimal
    tool_spans_total: int
    tool_spans_succeeded: int
    tool_success_rate: float | None


async def overview(
    session: AsyncSession,
    *,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    kind: str | None = None,
) -> StatsTotals:
    """窗口内（可选按 `kind` 收窄）的六项基础指标（3.2.9）。

    三条聚合查询：Run 计数 / 收敛 Run 的均值与合计 / tool span 成败（后两条都要能按 `kind` 收窄，
    所以 tool span 那条 join `traces`）。空窗口返回全 0 与 `null`，**不抛异常**（3.2.9 没定义错误分支）。
    """
    run_window = _window(Run.started_at, from_at, to_at)
    trace_window = _window(Trace.started_at, from_at, to_at)
    run_kind = [Run.kind == kind] if kind else []
    trace_kind = [Trace.kind == kind] if kind else []

    counts = (
        await session.execute(
            select(
                func.count(Run.id),
                _count_if(Run.status == str(RunStatus.SUCCEEDED)),
                _count_if(Run.status == str(RunStatus.FAILED)),
                _count_if(Run.status == str(RunStatus.CANCELED)),
                _count_if(Run.status.in_(_UNSETTLED)),
            ).where(*run_window, *run_kind)
        )
    ).one()
    total_runs, succeeded, failed, canceled, running = (int(value or 0) for value in counts)
    terminal_runs = succeeded + failed + canceled

    settled = (
        await session.execute(
            select(
                func.avg(Run.latency_ms),
                func.avg(Run.tool_call_count),
                func.avg(Run.total_tokens),
                func.coalesce(func.sum(Run.total_tokens), 0),
                func.coalesce(func.sum(Run.total_cost_usd), 0),
            ).where(*run_window, *run_kind, Run.status.in_(_TERMINAL))
        )
    ).one()
    avg_latency_ms, avg_tool_calls, avg_total_tokens, total_tokens, total_cost_usd = settled

    tool_spans = (
        await session.execute(
            select(func.count(Span.id), _count_if(Span.status == str(SpanStatus.OK)))
            .select_from(Span)
            .join(Trace, Span.trace_id == Trace.id)
            .where(Span.span_type == str(SpanType.TOOL), *trace_window, *trace_kind)
        )
    ).one()
    tool_spans_total, tool_spans_succeeded = (int(value or 0) for value in tool_spans)

    return StatsTotals(
        generated_at=datetime.now(UTC),
        from_at=from_at,
        to_at=to_at,
        kind=kind,
        total_runs=total_runs,
        succeeded=succeeded,
        failed=failed,
        canceled=canceled,
        running=running,
        terminal_runs=terminal_runs,
        success_rate=_rate(succeeded, terminal_runs),
        error_rate=_rate(failed, terminal_runs),
        avg_latency_ms=_mean(avg_latency_ms, digits=1),
        avg_tool_calls=_mean(avg_tool_calls, digits=2),
        avg_total_tokens=_mean(avg_total_tokens, digits=1),
        total_tokens=int(total_tokens or 0),
        total_cost_usd=Decimal(total_cost_usd or 0),
        tool_spans_total=tool_spans_total,
        tool_spans_succeeded=tool_spans_succeeded,
        tool_success_rate=_rate(tool_spans_succeeded, tool_spans_total),
    )


def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """`SUM(CASE WHEN <条件> THEN 1 ELSE 0 END)` —— 一次查询拿多个状态计数。"""
    return func.sum(case((condition, 1), else_=0))


def _window(
    column: InstrumentedAttribute[datetime],
    from_at: datetime | None,
    to_at: datetime | None,
) -> list[ColumnElement[bool]]:
    """`from` / `to` 的闭区间条件（库里的列是 naive UTC，0.2.1）。"""
    filters: list[ColumnElement[bool]] = []
    if from_at is not None:
        filters.append(column >= to_naive_utc(from_at))
    if to_at is not None:
        filters.append(column <= to_naive_utc(to_at))
    return filters


def _rate(numerator: int, denominator: int) -> float | None:
    """比率（分母为 0 → `None`，与 4.9.2 的 `workflow_success_rate` 同为 `null`）。"""
    return round(numerator / denominator, 4) if denominator else None


def _mean(value: float | Decimal | None, *, digits: int) -> float | None:
    """均值（无样本 → `None`）；小数位与 4.9.2 的报告一致（时延/token 1 位、工具调用 2 位）。"""
    return round(float(value), digits) if value is not None else None
