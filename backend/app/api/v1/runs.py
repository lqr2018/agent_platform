"""Run 路由（详细设计 3.2.4）。

（`GET /runs/{id}/tool-invocations` 属 Phase 2，本阶段不注册路由，SD-14②。）
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ApiResponse
from app.schemas.conversation import RunRead
from app.services import run_service

router = APIRouter(prefix="/runs", tags=["runs"])


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
