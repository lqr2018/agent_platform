"""Run 路由（详细设计 3.2.4 / 3.2.7）。

- `runs.router`：Chat / 评测的通用 Run（`GET /runs`、`GET /runs/{id}`、`POST /runs/{id}/cancel`）；
- `runs.workflow_router`：**workflow-run 相关**的五个端点（3.2.7 交付物要求的落点），
  `/workflow-runs` 前缀 + 轮询式进度（3.4：Workflow 运行不返回 SSE）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep, SettingsDep
from app.schemas.common import ApiResponse
from app.schemas.conversation import RunRead
from app.schemas.workflow import NodeRunRead, WorkflowRunRead
from app.services import run_service, workflow_service

router = APIRouter(prefix="/runs", tags=["runs"])

workflow_router = APIRouter(prefix="/workflow-runs", tags=["workflow-runs"])


@router.get("", response_model=ApiResponse[list[RunRead]], summary="运行列表（?kind=&agent_id=&status=）")
async def list_runs(
    session: SessionDep,
    kind: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    status_filter: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[list[RunRead]]:
    rows, total = await run_service.list_runs(
        session,
        kind=kind,
        agent_id=agent_id,
        conversation_id=conversation_id,
        status=status_filter,
        page=page,
        page_size=page_size,
    )
    return ApiResponse[list[RunRead]].of(
        [RunRead.model_validate(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/{run_id}", response_model=ApiResponse[RunRead], summary="运行详情（含 steps / token / cost）")
async def get_run(run_id: str, session: SessionDep) -> ApiResponse[RunRead]:
    run = await run_service.get_run(session, run_id)
    return ApiResponse[RunRead].of(RunRead.model_validate(run))


@router.post("/{run_id}/cancel", response_model=ApiResponse[RunRead], summary="取消运行中的 Run")
async def cancel_run(run_id: str, session: SessionDep) -> ApiResponse[RunRead]:
    run = await run_service.cancel_run(session, run_id)
    return ApiResponse[RunRead].of(RunRead.model_validate(run))


# --------------------------------------------------------------------------------------
# WorkflowRun（3.2.7）
# --------------------------------------------------------------------------------------
@workflow_router.get(
    "",
    response_model=ApiResponse[list[WorkflowRunRead]],
    summary="Workflow 运行列表（?workflow_id=&status=）",
)
async def list_workflow_runs(
    session: SessionDep,
    workflow_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[list[WorkflowRunRead]]:
    rows, total = await workflow_service.list_runs(
        session, workflow_id=workflow_id, status=status_filter, page=page, page_size=page_size
    )
    return ApiResponse[list[WorkflowRunRead]].of(
        [WorkflowRunRead.model_validate(row) for row in rows], page=page, page_size=page_size, total=total
    )


@workflow_router.get(
    "/{run_id}",
    response_model=ApiResponse[WorkflowRunRead],
    summary="运行详情（含 state 与当前节点）",
)
async def get_workflow_run(run_id: str, session: SessionDep) -> ApiResponse[WorkflowRunRead]:
    row = await workflow_service.get_run(session, run_id)
    return ApiResponse[WorkflowRunRead].of(WorkflowRunRead.model_validate(row))


@workflow_router.get(
    "/{run_id}/node-runs",
    response_model=ApiResponse[list[NodeRunRead]],
    summary="节点执行记录（按 seq 排序）",
)
async def list_node_runs(run_id: str, session: SessionDep) -> ApiResponse[list[NodeRunRead]]:
    rows = await workflow_service.list_node_runs(session, run_id)
    return ApiResponse[list[NodeRunRead]].of([NodeRunRead.model_validate(row) for row in rows])


@workflow_router.post(
    "/{run_id}/resume",
    response_model=ApiResponse[WorkflowRunRead],
    summary="从 checkpoint 续跑（4.5.4 的两类场景）",
)
async def resume_workflow_run(run_id: str, session: SessionDep, settings: SettingsDep) -> ApiResponse[WorkflowRunRead]:
    row = await workflow_service.resume_run(session, settings, run_id)
    return ApiResponse[WorkflowRunRead].of(WorkflowRunRead.model_validate(row))


@workflow_router.post(
    "/{run_id}/cancel",
    response_model=ApiResponse[WorkflowRunRead],
    summary="取消运行中的 WorkflowRun",
)
async def cancel_workflow_run(run_id: str, session: SessionDep, settings: SettingsDep) -> ApiResponse[WorkflowRunRead]:
    row = await workflow_service.cancel_run(session, run_id, settings=settings)
    return ApiResponse[WorkflowRunRead].of(WorkflowRunRead.model_validate(row))
