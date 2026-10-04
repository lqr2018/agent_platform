"""工具路由（详细设计 3.2.3 / 7.3）。

Phase 2 MVP 实现**只读**端点：`GET /tools`、`GET /tools/{id}`、`GET /tool-invocations`
（3.2.3 的三行 GET）。写端点（`POST` / `PATCH` / `DELETE` / `POST /{id}/test`）留给工具运营
能力的后续迭代：内置工具由迁移 `0003` + 启动对齐维护（4.2.2），MVP 前端只读展示。
审批端点（`/approvals*`）属 Backlog 迭代 C（SD-17），不在此注册。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ApiResponse
from app.schemas.tools import ToolDetail, ToolInvocationRead, ToolRead
from app.services import tool_service

router = APIRouter(tags=["tools"])


@router.get(
    "/tools",
    response_model=ApiResponse[list[ToolRead]],
    summary="工具列表（?tool_type=&status=&q=）",
)
async def list_tools(
    session: SessionDep,
    tool_type: str | None = Query(default=None, description="`builtin` / `api`"),
    status_filter: str | None = Query(default=None, alias="status", description="`enabled` / `disabled`"),
    q: str | None = Query(default=None, description="按 name / display_name 模糊匹配"),
) -> ApiResponse[list[ToolRead]]:
    """3.2.3：内置在前、按名称排序；`?status=` 用 `Query(alias=...)` 暴露（同 `traces.py` 的 `from`/`to`）。"""
    rows = await tool_service.list_tools(session, tool_type=tool_type, status=status_filter, q=q)
    return ApiResponse[list[ToolRead]].of([ToolRead.model_validate(row) for row in rows])


@router.get(
    "/tools/{tool_id}",
    response_model=ApiResponse[ToolDetail],
    summary="工具详情（含 http_config）",
)
async def get_tool(tool_id: str, session: SessionDep) -> ApiResponse[ToolDetail]:
    """不存在 → `TOOL_NOT_FOUND`（404）。"""
    row = await tool_service.get_tool(session, tool_id)
    return ApiResponse[ToolDetail].of(ToolDetail.model_validate(row))


@router.get(
    "/tool-invocations",
    response_model=ApiResponse[list[ToolInvocationRead]],
    summary="工具调用明细（?run_id=&tool_id=）",
)
async def list_tool_invocations(
    session: SessionDep,
    run_id: str | None = None,
    tool_id: str | None = None,
) -> ApiResponse[list[ToolInvocationRead]]:
    """2.5：Trace 之外仍可 SQL 查询的审计行；最新在前。"""
    rows = await tool_service.list_tool_invocations(session, run_id=run_id, tool_id=tool_id)
    return ApiResponse[list[ToolInvocationRead]].of([ToolInvocationRead.model_validate(row) for row in rows])
