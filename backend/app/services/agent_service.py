"""Agent 的 CRUD 与 Prompt 版本（详细设计 3.2.2 / 2.4）。

- 软删除：`deleted_at`（2.2：仅 `agents` / `knowledge_bases` 用软删除）；
- `system_prompt` 变化 → 先把**旧值**落 `agent_prompt_versions`（`version = 当前值`），
  再更新 `agents` 并把 `prompt_version += 1`（2.4 的写入约定）；
- Phase 2 的引用校验：`tool_ids` 必须指向**存在且 enabled** 的工具（4.2.2）；
- Phase 3 的引用校验：`workflow_id` 必须指向**存在且 published** 的 Workflow（2.9：Agent 绑的是
  一个已发布的版本，`draft` 只能试跑不能被 Agent 引用）；
  `knowledge_base_ids` 仍必须为空（Phase 5 落地，SD-14②：不为未实现的能力留入口）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import WorkflowStatus
from app.core.errors import AgentInvalidConfigError, AgentNotFoundError, ConflictError
from app.db.models import Agent, AgentPromptVersion, ModelProvider, Tool, Workflow
from app.db.models.agent import AGENT_STATUS_ENABLED, default_memory_config
from app.db.models.tool import TOOL_STATUS_ENABLED
from app.runtime.agent.state import AgentSpec
from app.schemas.agent import AgentCloneRequest, AgentCreate, AgentUpdate

UNSUPPORTED_FIELDS = {
    "knowledge_base_ids": "KNOWLEDGE_BASES_NOT_AVAILABLE_IN_PHASE_5",
}
"""本 build 不允许的引用字段 → `AGENT_INVALID_CONFIG.details.reason`（供前端提示）。

Phase 3 起 `workflow_id` 已经可用（见 `_validate_workflow`），故从本表移除。
"""


async def get_agent(session: AsyncSession, agent_id: str, *, include_deleted: bool = False) -> Agent:
    """取 Agent（默认排除软删除行）；不存在 → `AGENT_NOT_FOUND`（404）。"""
    agent = await session.get(Agent, agent_id)
    if agent is None or (agent.deleted_at is not None and not include_deleted):
        raise AgentNotFoundError(f"Agent '{agent_id}' does not exist")
    return agent


async def list_agents(
    session: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 100,
) -> Sequence[Agent]:
    """列表（3.2.2：`?q=&status=&tag=`）。"""
    statement = select(Agent).where(Agent.deleted_at.is_(None)).order_by(Agent.created_at.desc()).limit(limit)
    if q:
        statement = statement.where(Agent.name.contains(q))
    if status:
        statement = statement.where(Agent.status == status)
    rows = (await session.execute(statement)).scalars().all()
    if tag:
        return [row for row in rows if tag in (row.tags or [])]
    return rows


async def create_agent(session: AsyncSession, data: AgentCreate) -> Agent:
    """新建 Agent（校验引用与白名单，3.2.2 / 3.3.1）。"""
    _reject_unsupported_fields(knowledge_base_ids=data.knowledge_base_ids)
    await _validate_model(session, provider_id=data.model_provider_id, model_name=data.model_name)
    await _validate_tools(session, data.tool_ids)
    await _validate_workflow(session, data.workflow_id)
    await _ensure_name_available(session, data.name)

    agent = Agent(
        name=data.name,
        description=data.description,
        status=data.status,
        model_provider_id=data.model_provider_id,
        model_name=data.model_name,
        model_params=dict(data.model_params),
        system_prompt=data.system_prompt,
        tool_ids=list(data.tool_ids),
        knowledge_base_ids=list(data.knowledge_base_ids),
        memory_config=data.memory_config.model_dump(),
        workflow_id=data.workflow_id,
        max_steps=data.max_steps,
        timeout_seconds=data.timeout_seconds,
        tags=list(data.tags),
        is_template=data.is_template,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def update_agent(session: AsyncSession, agent_id: str, data: AgentUpdate) -> Agent:
    """`PATCH`：改 `system_prompt` 时先落旧版本快照（2.4）。"""
    agent = await get_agent(session, agent_id)
    changes = data.model_dump(exclude_unset=True)
    changes.pop("prompt_note", None)

    if "model_provider_id" in changes or "model_name" in changes:
        await _validate_model(
            session,
            provider_id=str(changes.get("model_provider_id", agent.model_provider_id)),
            model_name=str(changes.get("model_name", agent.model_name)),
        )

    name = changes.get("name")
    if name is not None and name != agent.name:
        await _ensure_name_available(session, name)

    if "workflow_id" in changes:
        await _validate_workflow(session, changes["workflow_id"])

    new_prompt = changes.pop("system_prompt", None)
    if new_prompt is not None and new_prompt != agent.system_prompt:
        session.add(
            AgentPromptVersion(
                agent_id=agent.id,
                version=agent.prompt_version,
                system_prompt=agent.system_prompt,
                note=(data.prompt_note or "").strip() or "previous version",
            )
        )
        agent.system_prompt = new_prompt
        agent.prompt_version += 1

    for field, raw_value in changes.items():
        value = raw_value
        if field == "memory_config" and raw_value is not None and hasattr(raw_value, "model_dump"):
            value = raw_value.model_dump()
        setattr(agent, field, value)

    await session.commit()
    await session.refresh(agent)
    return agent


async def delete_agent(session: AsyncSession, agent_id: str) -> None:
    """软删除（2.2 / 3.2.2）：置 `deleted_at` 并停用。"""
    agent = await get_agent(session, agent_id)
    agent.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    agent.status = "disabled"
    await session.commit()


async def clone_agent(session: AsyncSession, agent_id: str, data: AgentCloneRequest) -> Agent:
    """复制为新 Agent（3.2.2）；新 Agent 的 Prompt 版本从 1 重新开始。"""
    source = await get_agent(session, agent_id)
    await _ensure_name_available(session, data.name)
    clone = Agent(
        name=data.name,
        description=data.description if data.description is not None else source.description,
        status=AGENT_STATUS_ENABLED,
        model_provider_id=source.model_provider_id,
        model_name=source.model_name,
        model_params=dict(source.model_params or {}),
        system_prompt=source.system_prompt,
        prompt_version=1,
        tool_ids=list(source.tool_ids or []),
        knowledge_base_ids=list(source.knowledge_base_ids or []),
        memory_config=dict(source.memory_config or default_memory_config()),
        workflow_id=source.workflow_id,
        max_steps=source.max_steps,
        timeout_seconds=source.timeout_seconds,
        tags=list(source.tags or []),
        is_template=False,
    )
    session.add(clone)
    await session.commit()
    await session.refresh(clone)
    return clone


async def list_prompt_versions(session: AsyncSession, agent_id: str) -> Sequence[AgentPromptVersion]:
    """历史 Prompt（3.2.2 的"余量内实现"部分；回滚端点延后，见 7.0.1）。"""
    await get_agent(session, agent_id)
    statement = (
        select(AgentPromptVersion)
        .where(AgentPromptVersion.agent_id == agent_id)
        .order_by(AgentPromptVersion.version.desc())
    )
    return (await session.execute(statement)).scalars().all()


def to_spec(agent: Agent) -> AgentSpec:
    """ORM 行 → runtime 快照（4.4.1；runtime 不 import ORM，1.2）。"""
    return AgentSpec(
        id=agent.id,
        name=agent.name,
        model_provider_id=agent.model_provider_id,
        model_name=agent.model_name,
        system_prompt=agent.system_prompt,
        prompt_version=agent.prompt_version,
        model_params=dict(agent.model_params or {}),
        tool_ids=tuple(agent.tool_ids or ()),
        knowledge_base_ids=tuple(agent.knowledge_base_ids or ()),
        memory_config=dict(agent.memory_config or {}),
        workflow_id=agent.workflow_id,
        max_steps=agent.max_steps,
        timeout_seconds=agent.timeout_seconds,
        status=agent.status,
        tags=tuple(agent.tags or ()),
    )


async def _validate_model(session: AsyncSession, *, provider_id: str, model_name: str) -> None:
    """provider 必须存在；`model_name` 必须在白名单内（2.4）。"""
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise AgentInvalidConfigError(
            f"Model provider '{provider_id}' does not exist", details={"field": "model_provider_id"}
        )
    whitelist = {str(item.get("name")) for item in (provider.models or []) if item.get("name")}
    if whitelist and model_name not in whitelist:
        raise AgentInvalidConfigError(
            f"Model '{model_name}' is not in the provider whitelist",
            details={"field": "model_name", "allowed": sorted(whitelist)},
        )


async def _ensure_name_available(session: AsyncSession, name: str) -> None:
    exists = (await session.execute(select(Agent.id).where(Agent.name == name, Agent.deleted_at.is_(None)))).first()
    if exists:
        raise ConflictError(f"Agent name '{name}' already exists", details={"name": name})


def _reject_unsupported_fields(*, knowledge_base_ids: Sequence[str]) -> None:
    """不接受知识库引用（SD-14②：能力在 Phase 5 落地）。"""
    if knowledge_base_ids:
        raise AgentInvalidConfigError(
            "This build does not support knowledge bases yet (Phase 5)",
            details={
                "reason": "PHASE_NOT_SUPPORTED",
                "fields": {"knowledge_base_ids": UNSUPPORTED_FIELDS["knowledge_base_ids"]},
            },
        )


async def _validate_workflow(session: AsyncSession, workflow_id: str | None) -> None:
    """Phase 3：`workflow_id` 必须指向**存在且 published** 的 Workflow（2.9）。

    只有 `published` 能被 Agent 绑定：`draft` 表示"定义还在改"，绑定它会让 Agent 的行为随编辑漂移；
    想在编辑器里试跑 draft，用 `POST /workflows/{id}/runs`（3.2.7），不必先绑给 Agent。
    """
    if not workflow_id:
        return
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise AgentInvalidConfigError(f"Workflow '{workflow_id}' does not exist", details={"field": "workflow_id"})
    if workflow.status != str(WorkflowStatus.PUBLISHED):
        raise AgentInvalidConfigError(
            f"Workflow '{workflow.name}' is '{workflow.status}'; only published workflows can be bound",
            details={"field": "workflow_id", "status": workflow.status},
        )


async def _validate_tools(session: AsyncSession, tool_ids: Sequence[str]) -> None:
    """Phase 2：`tool_ids` 必须指向**存在且 enabled** 的工具（2.4 / 4.2.2）。

    被禁用 / 不存在的工具在 Agent 创建期就拒绝，避免运行期"sandbox 里永远拿不到工具"的隐性失败。
    """
    if not tool_ids:
        return
    rows = (await session.execute(select(Tool).where(Tool.id.in_(tuple(tool_ids))))).scalars().all()
    by_id = {row.id: row for row in rows}
    unknown = [tool_id for tool_id in tool_ids if tool_id not in by_id]
    if unknown:
        raise AgentInvalidConfigError(
            f"Unknown tool(s): {', '.join(unknown)}", details={"field": "tool_ids", "unknown": unknown}
        )
    disabled = [tool_id for tool_id in tool_ids if by_id[tool_id].status != TOOL_STATUS_ENABLED]
    if disabled:
        raise AgentInvalidConfigError(
            f"Tool(s) are disabled: {', '.join(disabled)}",
            details={"field": "tool_ids", "disabled": disabled},
        )
