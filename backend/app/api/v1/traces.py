"""Trace 只读路由（详细设计 3.2.9 / 4.8 / 7.8 任务 6）。

- `GET /traces`：游标分页列表；`GET /traces/{id}`：概要 + 扁平 span（键集分页，前端自行建树）；
- `GET /traces/{id}/tree`：服务端建好的嵌套树（7.8 任务 6）；
- `GET /spans/{id}`：单个 span 的完整 `input` / `output`；
- `GET /stats/overview` 在 `api/v1/stats.py`（同为 7.8 的 MVP 项）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.core.errors import ValidationError
from app.schemas.common import ApiResponse
from app.schemas.trace import SpanNode, SpanRead, SpanSummary, TraceDetail, TraceRead, TraceTree
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


@router.get(
    "/traces/{trace_id}",
    response_model=ApiResponse[TraceDetail],
    summary="Trace 概要 + 扁平 span 列表（键集分页）",
)
async def get_trace(
    trace_id: str,
    session: SessionDep,
    limit: int = Query(default=trace_service.SPAN_PAGE_LIMIT, ge=1, le=trace_service.SPAN_PAGE_MAX),
    cursor: str | None = None,
) -> ApiResponse[TraceDetail]:
    """3.2.9 的扁平 span 列表；`limit` / `cursor` 是 7.8 任务 6 的 spans 分页（游标即 `seq`）。"""
    trace = await trace_service.get_trace(session, trace_id)
    spans, next_cursor = await trace_service.list_spans(
        session, trace_id, limit=limit, cursor=_validate_span_cursor(cursor)
    )
    detail = TraceDetail(
        trace=TraceRead.model_validate(trace),
        spans=[SpanSummary.model_validate(row) for row in spans],
    )
    return ApiResponse[TraceDetail].of(detail, total=len(spans), next_cursor=next_cursor)


@router.get(
    "/traces/{trace_id}/tree",
    response_model=ApiResponse[TraceTree],
    summary="Trace 嵌套树（服务端建树，3.2.9 / 7.8 任务 6）",
)
async def get_trace_tree(trace_id: str, session: SessionDep) -> ApiResponse[TraceTree]:
    """`run → agent / workflow → llm / tool / retriever` 的嵌套结构（4.8.1）。

    树**不参与分页**：最多返回 `trace_service.SPAN_TREE_MAX_NODES` 个节点，超出或数据里有不可达
    span 时 `truncated=true`（前端据此提示"仅显示前 N 个节点 + 用扁平接口翻页"）。
    """
    trace = await trace_service.get_trace(session, trace_id)
    tree = await trace_service.load_trace_tree(session, trace)
    return ApiResponse[TraceTree].of(
        TraceTree(
            trace=TraceRead.model_validate(trace),
            roots=[_to_span_node(node) for node in tree.roots],
            node_count=tree.node_count,
            truncated=tree.truncated,
        ),
        total=tree.node_count,
    )


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


def _validate_span_cursor(cursor: str | None) -> str | None:
    """spans 游标格式非法 → `VALIDATION_ERROR`（422）。"""
    if not cursor:
        return None
    try:
        trace_service.decode_span_cursor(cursor)
    except (TypeError, ValueError) as exc:
        raise ValidationError("`cursor` is not a valid span cursor", details={"cursor": cursor}) from exc
    return cursor


def _to_span_node(node: trace_service.SpanTreeNode) -> SpanNode:
    """服务层树节点 → DTO（递归；4.8.1 的层级最多四层，递归深度 = 树深）。"""
    summary = SpanSummary.model_validate(node.span)
    return SpanNode(**summary.model_dump(), children=[_to_span_node(child) for child in node.children])
