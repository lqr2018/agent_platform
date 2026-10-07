"""Agent API（详细设计 3.2.2 / 2.4：CRUD、软删除、Prompt 版本、引用校验）。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentPromptVersion, Tool
from app.runtime.tools.registry import builtin_tool_id
from tests.helpers import create_fake_provider

AGENT_BODY = {
    "name": "研究助手",
    "description": "搜索 + 总结",
    "model_name": "fake-model",
    "system_prompt": "你是一名研究员。",
    "tags": ["demo", "research"],
}


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


@pytest_asyncio.fixture
async def provider_id(db_session: AsyncSession) -> str:
    provider = await create_fake_provider(db_session, name="agent-provider")
    return str(provider.id)


async def _create(app_client: AsyncClient, provider_id: str, **overrides: object) -> dict:
    body = {**AGENT_BODY, "model_provider_id": provider_id, **overrides}
    response = await app_client.post("/api/v1/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_create_and_read_agent(app_client: AsyncClient, provider_id: str) -> None:
    data = await _create(app_client, provider_id)
    assert data["prompt_version"] == 1
    assert data["status"] == "enabled"
    assert data["tags"] == ["demo", "research"]
    assert data["memory_config"]["short_term"]["strategy"] == "window"
    assert data["tool_ids"] == [] and data["knowledge_base_ids"] == [] and data["workflow_id"] is None

    listing = await app_client.get("/api/v1/agents", params={"tag": "research"})
    assert [row["id"] for row in listing.json()["data"]] == [data["id"]]
    assert (await app_client.get("/api/v1/agents", params={"q": "不存在"})).json()["data"] == []


@pytest.mark.asyncio
async def test_patch_prompt_writes_history_and_bumps_version(
    app_client: AsyncClient, provider_id: str, db_session: AsyncSession
) -> None:
    """2.4：改 `system_prompt` → 先落旧值快照，再自增 `prompt_version`。"""
    data = await _create(app_client, provider_id)

    patched = await app_client.patch(
        f"/api/v1/agents/{data['id']}", json={"system_prompt": "你是资深研究员。", "prompt_note": "加强语气"}
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["prompt_version"] == 2

    versions = (await app_client.get(f"/api/v1/agents/{data['id']}/prompt-versions")).json()["data"]
    assert len(versions) == 1
    assert versions[0]["version"] == 1 and versions[0]["system_prompt"] == "你是一名研究员。"
    assert versions[0]["note"] == "加强语气"

    rows = (
        (await db_session.execute(select(AgentPromptVersion).where(AgentPromptVersion.agent_id == data["id"])))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_other_field_changes_do_not_bump_version(app_client: AsyncClient, provider_id: str) -> None:
    data = await _create(app_client, provider_id)
    patched = await app_client.patch(f"/api/v1/agents/{data['id']}", json={"description": "改个描述"})
    assert patched.json()["data"]["prompt_version"] == 1


@pytest.mark.asyncio
async def test_clone_and_soft_delete(app_client: AsyncClient, provider_id: str) -> None:
    data = await _create(app_client, provider_id)

    cloned = await app_client.post(f"/api/v1/agents/{data['id']}/clone", json={"name": "研究助手-副本"})
    assert cloned.status_code == 201
    assert cloned.json()["data"]["id"] != data["id"]
    assert cloned.json()["data"]["prompt_version"] == 1

    assert (await app_client.delete(f"/api/v1/agents/{data['id']}")).status_code == 204
    assert (await app_client.get(f"/api/v1/agents/{data['id']}")).status_code == 404
    listing = await app_client.get("/api/v1/agents")
    assert data["id"] not in [row["id"] for row in listing.json()["data"]]


@pytest.mark.asyncio
async def test_validation_rejects_unknown_provider_and_model(app_client: AsyncClient, provider_id: str) -> None:
    unknown_provider = await app_client.post(
        "/api/v1/agents", json={**AGENT_BODY, "model_provider_id": "01J8Z0000000000000000000ZZ"}
    )
    assert unknown_provider.status_code == 422
    assert unknown_provider.json()["error"]["code"] == "AGENT_INVALID_CONFIG"

    bad_model = await app_client.post(
        "/api/v1/agents", json={**AGENT_BODY, "model_provider_id": provider_id, "model_name": "not-in-whitelist"}
    )
    assert bad_model.status_code == 422
    assert bad_model.json()["error"]["details"]["field"] == "model_name"


@pytest.mark.asyncio
async def test_reference_validation_for_tools_and_workflows(
    app_client: AsyncClient, provider_id: str, db_session: AsyncSession
) -> None:
    """3.2.2 / Phase 2–3：`tool_ids` 必须指向 enabled 的工具；`workflow_id` 必须存在且已发布。"""
    unknown_id = "01J8Z0000000000000000000T1"
    unknown = await app_client.post(
        "/api/v1/agents",
        json={**AGENT_BODY, "model_provider_id": provider_id, "tool_ids": [unknown_id]},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"] == {"field": "tool_ids", "unknown": [unknown_id]}

    disabled_row = await db_session.get(Tool, builtin_tool_id("web_search"))
    assert disabled_row is not None
    disabled_row.status = "disabled"
    await db_session.commit()
    disabled = await app_client.post(
        "/api/v1/agents",
        json={
            **AGENT_BODY,
            "name": "disabled-tool-agent",
            "model_provider_id": provider_id,
            "tool_ids": [builtin_tool_id("web_search")],
        },
    )
    assert disabled.status_code == 422
    assert disabled.json()["error"]["details"] == {
        "field": "tool_ids",
        "disabled": [builtin_tool_id("web_search")],
    }

    created = await _create(app_client, provider_id, name="tool-agent", tool_ids=[builtin_tool_id("calculator")])
    assert created["tool_ids"] == [builtin_tool_id("calculator")]

    wf_body = {
        **AGENT_BODY,
        "name": "wf-agent",
        "model_provider_id": provider_id,
        "workflow_id": "01J8Z00000000000000000W1",
    }
    with_workflow = await app_client.post("/api/v1/agents", json=wf_body)
    assert with_workflow.status_code == 422
    assert with_workflow.json()["error"]["details"] == {"field": "workflow_id"}


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(app_client: AsyncClient, provider_id: str) -> None:
    await _create(app_client, provider_id)
    again = await app_client.post("/api/v1/agents", json={**AGENT_BODY, "model_provider_id": provider_id})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


@pytest.mark.asyncio
async def test_unknown_agent_returns_404(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/agents/01J8Z0000000000000000000AA")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"
