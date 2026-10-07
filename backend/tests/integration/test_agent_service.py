"""服务层直测：Agent（详细设计 2.4 / 3.2.2）。

与 `test_agents_api.py`（走 HTTP）互补：这里直接调 `agent_service`，把校验分支、软删除、
Prompt 快照与快照边界都覆盖到（9.3 的 `app/services/**` 覆盖率门禁）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AgentInvalidConfigError, AgentNotFoundError, ConflictError
from app.db.models import ModelProvider, Tool
from app.runtime.tools.registry import builtin_tool_id
from app.schemas.agent import AgentCloneRequest, AgentCreate, AgentUpdate
from app.services import agent_service
from tests.helpers import create_fake_provider


@pytest_asyncio.fixture
async def provider(session: AsyncSession) -> ModelProvider:
    return await create_fake_provider(session, name="svc-provider")


def _payload(provider: ModelProvider, **overrides: object) -> AgentCreate:
    data: dict[str, object] = {
        "name": "svc-agent",
        "model_provider_id": provider.id,
        "model_name": "fake-model",
        "system_prompt": "旧 prompt",
        "tags": ["demo"],
    }
    data.update(overrides)
    return AgentCreate(**data)


@pytest.mark.asyncio
async def test_create_get_and_list_filters(session: AsyncSession, provider: ModelProvider) -> None:
    created = await agent_service.create_agent(session, _payload(provider))
    assert created.prompt_version == 1 and created.status == "enabled"

    fetched = await agent_service.get_agent(session, created.id)
    assert fetched.id == created.id

    assert [row.id for row in await agent_service.list_agents(session)] == [created.id]
    assert [row.id for row in await agent_service.list_agents(session, q="svc-a")] == [created.id]
    assert await agent_service.list_agents(session, q="nope") == []
    assert [row.id for row in await agent_service.list_agents(session, status="enabled")] == [created.id]
    assert await agent_service.list_agents(session, status="disabled") == []
    assert [row.id for row in await agent_service.list_agents(session, tag="demo")] == [created.id]
    assert await agent_service.list_agents(session, tag="other") == []


@pytest.mark.asyncio
async def test_unknown_agent_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(AgentNotFoundError):
        await agent_service.get_agent(session, "01J8Z0000000000000000000ZZ")


@pytest.mark.asyncio
async def test_create_rejects_unknown_provider_and_model(session: AsyncSession, provider: ModelProvider) -> None:
    with pytest.raises(AgentInvalidConfigError) as excinfo:
        await agent_service.create_agent(session, _payload(provider, model_provider_id="01J8Z0000000000000000000ZZ"))
    assert excinfo.value.details["field"] == "model_provider_id"

    with pytest.raises(AgentInvalidConfigError) as model_error:
        await agent_service.create_agent(session, _payload(provider, model_name="unknown-model"))
    assert model_error.value.details["allowed"] == ["fake-model"]


@pytest.mark.asyncio
async def test_create_validates_tool_references(session: AsyncSession, provider: ModelProvider) -> None:
    """Phase 2–3：`tool_ids` 必须存在且 enabled（4.2.2）；`workflow_id` 必须存在且已发布（2.9）。"""
    with pytest.raises(AgentInvalidConfigError) as excinfo:
        await agent_service.create_agent(session, _payload(provider, tool_ids=["01J8Z0000000000000000000T1"]))
    assert excinfo.value.details == {"field": "tool_ids", "unknown": ["01J8Z0000000000000000000T1"]}

    disabled = await session.get(Tool, builtin_tool_id("web_search"))
    assert disabled is not None
    disabled.status = "disabled"
    await session.commit()
    with pytest.raises(AgentInvalidConfigError) as disabled_error:
        await agent_service.create_agent(
            session, _payload(provider, name="disabled-agent", tool_ids=[builtin_tool_id("web_search")])
        )
    assert disabled_error.value.details == {
        "field": "tool_ids",
        "disabled": [builtin_tool_id("web_search")],
    }

    with pytest.raises(AgentInvalidConfigError) as wf_error:
        await agent_service.create_agent(
            session, _payload(provider, name="wf-agent", workflow_id="01J8Z00000000000000000W1")
        )
    assert wf_error.value.details == {"field": "workflow_id"}

    created = await agent_service.create_agent(
        session, _payload(provider, name="tool-agent", tool_ids=[builtin_tool_id("calculator")])
    )
    assert list(created.tool_ids) == [builtin_tool_id("calculator")]


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(session: AsyncSession, provider: ModelProvider) -> None:
    await agent_service.create_agent(session, _payload(provider))
    with pytest.raises(ConflictError):
        await agent_service.create_agent(session, _payload(provider))


@pytest.mark.asyncio
async def test_update_prompt_snapshot_and_validation(session: AsyncSession, provider: ModelProvider) -> None:
    created = await agent_service.create_agent(session, _payload(provider))

    updated = await agent_service.update_agent(
        session, created.id, AgentUpdate.model_validate({"system_prompt": "新 prompt", "prompt_note": "改语气"})
    )
    assert updated.prompt_version == 2 and updated.system_prompt == "新 prompt"

    versions = await agent_service.list_prompt_versions(session, created.id)
    assert [(row.version, row.system_prompt, row.note) for row in versions] == [(1, "旧 prompt", "改语气")]

    # 幂等：提交同样的 prompt 不再自增（2.4 只对"变化"留档）
    same = await agent_service.update_agent(
        session, created.id, AgentUpdate.model_validate({"system_prompt": "新 prompt"})
    )
    assert same.prompt_version == 2

    # 改模型时重新校验白名单
    with pytest.raises(AgentInvalidConfigError):
        await agent_service.update_agent(session, created.id, AgentUpdate.model_validate({"model_name": "bad-model"}))

    # 改名冲突（另建一个 Agent）
    other = await agent_service.create_agent(session, _payload(provider, name="svc-agent-2"))
    with pytest.raises(ConflictError):
        await agent_service.update_agent(session, other.id, AgentUpdate.model_validate({"name": "svc-agent"}))


@pytest.mark.asyncio
async def test_soft_delete_hides_agent(session: AsyncSession, provider: ModelProvider) -> None:
    created = await agent_service.create_agent(session, _payload(provider))
    await agent_service.delete_agent(session, created.id)

    assert await agent_service.list_agents(session) == []
    with pytest.raises(AgentNotFoundError):
        await agent_service.get_agent(session, created.id)

    raw = await agent_service.get_agent(session, created.id, include_deleted=True)
    assert raw.deleted_at is not None and raw.status == "disabled"


@pytest.mark.asyncio
async def test_clone_copies_config_and_resets_prompt_version(session: AsyncSession, provider: ModelProvider) -> None:
    created = await agent_service.create_agent(session, _payload(provider))
    await agent_service.update_agent(session, created.id, AgentUpdate.model_validate({"system_prompt": "v2"}))

    clone = await agent_service.clone_agent(
        session, created.id, AgentCloneRequest(name="svc-agent-copy", description="副本")
    )
    assert clone.id != created.id
    assert clone.prompt_version == 1
    assert clone.system_prompt == "v2"
    assert clone.description == "副本"
    assert clone.tags == ["demo"]

    with pytest.raises(ConflictError):
        await agent_service.clone_agent(session, created.id, AgentCloneRequest(name="svc-agent"))


def test_to_spec_maps_row_to_snapshot() -> None:
    from app.db.models import Agent

    row = Agent(
        id="01J8Z0000000000000000000A1",
        name="spec-agent",
        model_provider_id="01J8Z0000000000000000000P1",
        model_name="fake-model",
        system_prompt="hi",
        prompt_version=3,
        tool_ids=[],
        knowledge_base_ids=[],
        memory_config={"short_term": {"max_turns": 2}},
        tags=["x"],
        max_steps=5,
        timeout_seconds=60,
    )
    spec = agent_service.to_spec(row)
    assert spec.id == row.id and spec.prompt_version == 3
    assert spec.short_term_config == {"max_turns": 2}
    assert spec.tags == ("x",) and spec.tool_ids == ()
