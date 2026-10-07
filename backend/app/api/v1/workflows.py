"""Workflow 路由（详细设计 3.2.7 / 7.4）。

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/workflows` | 列表（`?status=`，另支持 `?q=`） |
| POST | `/workflows` | 新建（`definition` 走图校验；重名 409 / 图非法 422） |
| GET / PATCH / DELETE | `/workflows/{id}` | 详情 / 更新（回 draft）/ 删除 |
| POST | `/workflows/{id}/validate` | 只校验不落库（**不合法也回 200**，错误清单给编辑器） |
| POST | `/workflows/{id}/publish` | `draft → published`，`version += 1` |
| POST | `/workflows/{id}/runs` | 启动运行 → `202` + `workflow_runs` |

`workflow-runs` 五个端点挂在 `runs.py`（3.2.7 交付物写的"`runs.py`（workflow-run 相关）"）。
审批端点（`/approvals*`）与 `human` 节点属 Backlog 迭代 C（SD-17），不在此注册。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, SettingsDep, idempotency_key
from app.db.models.workflow import TRIGGER_API
from app.schemas.common import ApiResponse
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowRead,
    WorkflowRunCreate,
    WorkflowRunRead,
    WorkflowUpdate,
    WorkflowValidateRequest,
    WorkflowValidateResult,
    WorkflowValidationIssue,
)
from app.services import workflow_service

router = APIRouter(prefix="/workflows", tags=["workflows"])

CREATE_SCOPE = "workflows.create"
"""`Idempotency-Key` 的作用域（1.5.5；同 `agents.create` 的写法）。"""


@router.get("", response_model=ApiResponse[list[WorkflowRead]], summary="Workflow 列表（?status=&q=）")
async def list_workflows(
    session: SessionDep,
    status_filter: str | None = Query(default=None, alias="status", description="draft / published / archived"),
    q: str | None = Query(default=None, description="按名称模糊匹配"),
) -> ApiResponse[list[WorkflowRead]]:
    rows = await workflow_service.list_workflows(session, status=status_filter, q=q)
    return ApiResponse[list[WorkflowRead]].of([WorkflowRead.model_validate(row) for row in rows])


@router.post(
    "",
    response_model=ApiResponse[WorkflowRead],
    status_code=status.HTTP_201_CREATED,
    summary="新建 Workflow（定义走图校验）",
)
async def create_workflow(
    payload: WorkflowCreate,
    request: Request,
    session: SessionDep,
    store: IdempotencyDep,
) -> ApiResponse[WorkflowRead] | JSONResponse:
    """图非法 → `WORKFLOW_INVALID_GRAPH`（422，`details.errors` 带错误清单，SD-1）。"""
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)

    row = await workflow_service.create_workflow(session, payload)
    response = ApiResponse[WorkflowRead].of(WorkflowRead.model_validate(row))
    if key:
        store.put(
            CREATE_SCOPE,
            key,
            status_code=status.HTTP_201_CREATED,
            payload=response.model_dump(mode="json"),
        )
    return response


@router.get("/{workflow_id}", response_model=ApiResponse[WorkflowRead], summary="Workflow 详情")
async def get_workflow(workflow_id: str, session: SessionDep) -> ApiResponse[WorkflowRead]:
    row = await workflow_service.get_workflow(session, workflow_id)
    return ApiResponse[WorkflowRead].of(WorkflowRead.model_validate(row))


@router.patch("/{workflow_id}", response_model=ApiResponse[WorkflowRead], summary="更新（定义变更回 draft）")
async def update_workflow(workflow_id: str, payload: WorkflowUpdate, session: SessionDep) -> ApiResponse[WorkflowRead]:
    row = await workflow_service.update_workflow(session, workflow_id, payload)
    return ApiResponse[WorkflowRead].of(WorkflowRead.model_validate(row))


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除（有运行中的 Run → 409）")
async def delete_workflow(workflow_id: str, session: SessionDep) -> None:
    await workflow_service.delete_workflow(session, workflow_id)


@router.post(
    "/{workflow_id}/validate",
    response_model=ApiResponse[WorkflowValidateResult],
    summary="只校验不落库（可带未保存的草稿）",
)
async def validate_workflow(
    workflow_id: str,
    session: SessionDep,
    payload: WorkflowValidateRequest | None = None,
) -> ApiResponse[WorkflowValidateResult]:
    """请求体可省略（校验已存定义）；带 `definition` 时校验**草稿**（编辑器"保存前先校验"）。"""
    definition = payload.definition if payload is not None else None
    if definition is None:
        row = await workflow_service.get_workflow(session, workflow_id)
        definition = dict(row.definition)
    errors, graph = await workflow_service.validate_definition(session, definition)
    return ApiResponse[WorkflowValidateResult].of(
        WorkflowValidateResult(
            valid=not errors,
            errors=[WorkflowValidationIssue(**item) for item in errors],
            graph=graph.public_definition() if graph is not None else None,
        )
    )


@router.post("/{workflow_id}/publish", response_model=ApiResponse[WorkflowRead], summary="发布（version +1）")
async def publish_workflow(workflow_id: str, session: SessionDep) -> ApiResponse[WorkflowRead]:
    row = await workflow_service.publish_workflow(session, workflow_id)
    return ApiResponse[WorkflowRead].of(WorkflowRead.model_validate(row))


@router.post(
    "/{workflow_id}/runs",
    response_model=ApiResponse[WorkflowRunRead],
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动运行 → 202（进度用 /workflow-runs/{id}/node-runs 轮询）",
)
async def start_workflow_run(
    workflow_id: str,
    session: SessionDep,
    settings: SettingsDep,
    payload: WorkflowRunCreate | None = None,
) -> ApiResponse[WorkflowRunRead]:
    """3.4：**不返回 SSE** —— 202 + 轮询（Workflow 可能是分钟级长任务）。"""
    row = await workflow_service.get_workflow(session, workflow_id)
    handle = await workflow_service.start_run(
        session,
        settings,
        workflow=row,
        payload=payload.input if payload is not None else {},
        trigger=TRIGGER_API,
    )
    workflow_run = await workflow_service.get_run(session, handle.run_id)
    return ApiResponse[WorkflowRunRead].of(WorkflowRunRead.model_validate(workflow_run))
