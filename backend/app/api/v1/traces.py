"""Trace 只读路由（详细设计 3.2.9 / 4.8）。

- `GET /traces`：游标分页列表；`GET /traces/{id}`：概要 + 扁平 span（前端自行建树）；
- `GET /spans/{id}`：单个 span 的完整 `input` / `output`；
- `GET /traces/{id}/tree` 属 M3、`GET /stats/overview` 属 M3，本阶段不注册（SD-14②）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.core.errors import ValidationError
from app.schemas.common import ApiResponse
from app.schemas.trace import SpanRead, SpanSummary, TraceDetail, TraceRead
from app.services import trace_service

router = APIRouter(tags=["traces"])


@router.get("/traces", response_model=ApiResponse[list[TraceRead]], summary="Trace 列表（游标分页）")
async def list_traces(
    session: SessionDep,
    kind: str | None = None,
    status_filter: str | None = None,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    limit: int = Query(default=trace_service.TRACE_PAGE_LIMIT, ge=1, le=trace_service.TRACE_PAGE_MAX),
    cursor: str | None = None,
) -> ApiResponse[list[TraceRead]]:
    rows, next_cursor = await trace_service.list_traces(
        session,
        kind=kind,
        status=status_filter,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
        cursor=_validate_cursor(cursor),
    )
    return ApiResponse[list[TraceRead]].of(
        [TraceRead.model_validate(row) for row in rows], total=len(rows), next_cursor=next_cursor
    )


@router.get("/traces/{trace_id}", response_model=ApiResponse[TraceDetail], summary="Trace 概要 + 扁平 span 列表")
async def get_trace(trace_id: str, session: SessionDep) -> ApiResponse[TraceDetail]:
    trace = await trace_service.get_trace(session, trace_id)
    spans = await trace_service.list_spans(session, trace_id)
    detail = TraceDetail(
        trace=TraceRead.model_validate(trace),
        spans=[SpanSummary.model_validate(row) for row in spans],
    )
    return ApiResponse[TraceDetail].of(detail)


@router.get("/spans/{span_id}", response_model=ApiResponse[SpanRead], summary="单个 span（含 input/output）")
async def get_span(span_id: str, session: SessionDep) -> ApiResponse[SpanRead]:
    span = await trace_service.get_span(session, span_id)
    return ApiResponse[SpanRead].of(SpanRead.model_validate(span))


def _validate_cursor(cursor: str | None) -> str | None:
    """游标格式非法 → `VALIDATION_ERROR`（422）。"""
    if not cursor:
        return None
    try:
        trace_service.decode_cursor(cursor)
    except (TypeError, ValueError) as exc:
        raise ValidationError("`cursor` is not a valid trace cursor", details={"cursor": cursor}) from exc
    return cursor
