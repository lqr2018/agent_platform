"""工具路由（详细设计 3.2.3 / 7.3）。

七个端点，覆盖 3.2.3 工具部分的全部行：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/tools` | 列表（`?tool_type=&status=&q=`） |
| POST | `/tools` | 新建 `api` 类型工具（内置由迁移 / 启动对齐维护） |
| GET | `/tools/{id}` | 详情（含 `http_config`；列表不含，避免请求头泄密） |
| PATCH | `/tools/{id}` | 更新（内置行只允许 `status` / `permission_config` / `tags`，2.5） |
| DELETE | `/tools/{id}` | 删除（内置禁删 → 409，2.5） |
| POST | `/tools/{id}/test` | 直接执行一次（跳过 LLM；走同一套九步流水线，失败也是 200 + `ok=false`） |
| GET | `/tool-invocations` | 调用明细（`?run_id=&tool_id=`） |

审批端点（`/approvals*`）属 Backlog 迭代 C（SD-17），不在此注册。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, SettingsDep, idempotency_key
from app.schemas.common import ApiResponse
from app.schemas.tools import (
    ToolCreate,
    ToolDetail,
    ToolInvocationRead,
    ToolRead,
    ToolTestRequest,
    ToolTestResult,
    ToolUpdate,
)
from app.services import tool_service

router = APIRouter(tags=["tools"])

CREATE_SCOPE = "tools.create"
"""`Idempotency-Key` 的作用域（1.5.5；同 `agents.create` 的写法）。"""


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


@router.post(
    "/tools",
    response_model=ApiResponse[ToolDetail],
    status_code=status.HTTP_201_CREATED,
    summary="新建 api 类型工具",
)
async def create_tool(
    payload: ToolCreate,
    request: Request,
    session: SessionDep,
    store: IdempotencyDep,
) -> ApiResponse[ToolDetail] | JSONResponse:
    """只建 `api` 类型（`tool_type` 收口为 `"api"`，其余取值 Pydantic 直接 422）。

    - 名称与内置工具（或既有工具）重名 → `CONFLICT`（409）；
    - `http_config.url` 缺失 / `method` 不在白名单 → `VALIDATION_ERROR`（422）。
    """
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)

    tool = await tool_service.create_tool(session, payload)
    response = ApiResponse[ToolDetail].of(ToolDetail.model_validate(tool))
    if key:
        store.put(CREATE_SCOPE, key, status_code=status.HTTP_201_CREATED, payload=response.model_dump(mode="json"))
    return response


@router.get(
    "/tools/{tool_id}",
    response_model=ApiResponse[ToolDetail],
    summary="工具详情（含 http_config）",
)
async def get_tool(tool_id: str, session: SessionDep) -> ApiResponse[ToolDetail]:
    """不存在 → `TOOL_NOT_FOUND`（404）。"""
    row = await tool_service.get_tool(session, tool_id)
    return ApiResponse[ToolDetail].of(ToolDetail.model_validate(row))


@router.patch(
    "/tools/{tool_id}",
    response_model=ApiResponse[ToolDetail],
    summary="更新工具（内置只允许 status / permission_config / tags）",
)
async def update_tool(tool_id: str, payload: ToolUpdate, session: SessionDep) -> ApiResponse[ToolDetail]:
    """2.5：内置工具"可禁用 / 可改权限"；改代码属地的列 → `VALIDATION_ERROR`（422）。"""
    row = await tool_service.update_tool(session, tool_id, payload)
    return ApiResponse[ToolDetail].of(ToolDetail.model_validate(row))


@router.delete("/tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除工具（内置禁删）")
async def delete_tool(tool_id: str, session: SessionDep) -> None:
    """`is_system=true` → `CONFLICT`（409）；历史 `tool_invocations` 的 `tool_id` 置空（2.5）。"""
    await tool_service.delete_tool(session, tool_id)


@router.post(
    "/tools/{tool_id}/test",
    response_model=ApiResponse[ToolTestResult],
    summary="直接执行一次（跳过 LLM）",
)
async def run_tool_test(
    tool_id: str,
    session: SessionDep,
    settings: SettingsDep,
    payload: ToolTestRequest | None = None,
) -> ApiResponse[ToolTestResult]:
    """请求体 `{arguments: {...}}` 可省略（等价于 `{}`）。

    执行失败（参数非法 / 被拒 / 沙箱越界 / 超时）沿 `ProviderTestResult` 的先例回 `200 + ok=false`，
    错误码与消息直接来自九步流水线，便于运营在页面上定位。
    """
    row, result = await tool_service.run_tool_test(
        session,
        tool_id,
        settings=settings,
        arguments=payload.arguments if payload is not None else {},
    )
    return ApiResponse[ToolTestResult].of(
        ToolTestResult(
            ok=result.succeeded,
            tool_id=row.id,
            tool_name=row.name,
            status=str(result.status),
            permission_decision=str(result.permission_decision),
            latency_ms=result.latency_ms,
            result=result.content,
            truncated=result.truncated,
            error_code=result.error_code,
            error_message=result.error_message,
            details={key: value for key, value in result.meta.items() if isinstance(value, (str, int, float, bool))},
        )
    )


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
