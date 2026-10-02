"""Agent 路由（详细设计 3.2.2 / 3.3.1）。

- `DELETE` 为软删除（2.4）；`clone` 复制为新 Agent；
- `prompt-versions` 只读列表（回滚端点延后，见 7.0.1 的收窄口径）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, idempotency_key
from app.schemas.agent import (
    AgentCloneRequest,
    AgentCreate,
    AgentPromptVersionRead,
    AgentRead,
    AgentUpdate,
)
from app.schemas.common import ApiResponse
from app.services import agent_service

router = APIRouter(prefix="/agents", tags=["agents"])

CREATE_SCOPE = "agents.create"


@router.get("", response_model=ApiResponse[list[AgentRead]], summary="Agent 列表（?q=&status=&tag=）")
async def list_agents(
    session: SessionDep,
    q: str | None = None,
    status_filter: str | None = None,
    tag: str | None = None,
) -> ApiResponse[list[AgentRead]]:
    rows = await agent_service.list_agents(session, q=q, status=status_filter, tag=tag)
    return ApiResponse[list[AgentRead]].of([AgentRead.model_validate(row) for row in rows])


@router.post(
    "",
    response_model=ApiResponse[AgentRead],
    status_code=status.HTTP_201_CREATED,
    summary="新建 Agent（校验引用与白名单）",
)
async def create_agent(
    payload: AgentCreate,
    request: Request,
    session: SessionDep,
    store: IdempotencyDep,
) -> ApiResponse[AgentRead] | JSONResponse:
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)

    agent = await agent_service.create_agent(session, payload)
    response = ApiResponse[AgentRead].of(AgentRead.model_validate(agent))
    if key:
        store.put(CREATE_SCOPE, key, status_code=status.HTTP_201_CREATED, payload=response.model_dump(mode="json"))
    return response


@router.get("/{agent_id}", response_model=ApiResponse[AgentRead], summary="Agent 详情")
async def get_agent(agent_id: str, session: SessionDep) -> ApiResponse[AgentRead]:
    agent = await agent_service.get_agent(session, agent_id)
    return ApiResponse[AgentRead].of(AgentRead.model_validate(agent))


@router.patch("/{agent_id}", response_model=ApiResponse[AgentRead], summary="更新 Agent（prompt 变更留档）")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    session: SessionDep,
) -> ApiResponse[AgentRead]:
    agent = await agent_service.update_agent(session, agent_id, payload)
    return ApiResponse[AgentRead].of(AgentRead.model_validate(agent))


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="软删除 Agent")
async def delete_agent(agent_id: str, session: SessionDep) -> None:
    await agent_service.delete_agent(session, agent_id)


@router.post(
    "/{agent_id}/clone",
    response_model=ApiResponse[AgentRead],
    status_code=status.HTTP_201_CREATED,
    summary="复制为新 Agent",
)
async def clone_agent(
    agent_id: str,
    payload: AgentCloneRequest,
    session: SessionDep,
) -> ApiResponse[AgentRead]:
    agent = await agent_service.clone_agent(session, agent_id, payload)
    return ApiResponse[AgentRead].of(AgentRead.model_validate(agent))


@router.get(
    "/{agent_id}/prompt-versions",
    response_model=ApiResponse[list[AgentPromptVersionRead]],
    summary="Prompt 历史版本",
)
async def list_prompt_versions(
    agent_id: str,
    session: SessionDep,
) -> ApiResponse[list[AgentPromptVersionRead]]:
    rows = await agent_service.list_prompt_versions(session, agent_id)
    return ApiResponse[list[AgentPromptVersionRead]].of([AgentPromptVersionRead.model_validate(row) for row in rows])
