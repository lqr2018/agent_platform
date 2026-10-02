"""会话与消息 API（详细设计 3.2.4 / 2.6：CRUD、游标分页、会话级保护）。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import run_service
from tests.helpers import collect_sse, create_agent, create_fake_provider


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


@pytest_asyncio.fixture
async def agent(app_client: AsyncClient, db_session: AsyncSession) -> dict:
    provider = await create_fake_provider(db_session, name="conv-provider")
    return await create_agent(app_client, provider_id=str(provider.id), name="conv-agent")


@pytest.mark.asyncio
async def test_conversation_crud(app_client: AsyncClient, agent: dict) -> None:
    created = await app_client.post("/api/v1/conversations", json={"agent_id": agent["id"]})
    assert created.status_code == 201
    conversation = created.json()["data"]
    assert conversation["owner_key"] == "local"  # SD-3
    assert conversation["status"] == "active" and conversation["message_count"] == 0

    renamed = await app_client.patch(
        f"/api/v1/conversations/{conversation['id']}", json={"title": "改过的标题", "status": "archived"}
    )
    assert renamed.json()["data"]["title"] == "改过的标题"
    assert renamed.json()["data"]["status"] == "archived"

    listing = await app_client.get("/api/v1/conversations", params={"agent_id": agent["id"]})
    body = listing.json()
    assert body["meta"]["total"] == 1 and body["meta"]["page"] == 1
    assert body["data"][0]["id"] == conversation["id"]

    assert (await app_client.delete(f"/api/v1/conversations/{conversation['id']}")).status_code == 204
    assert (await app_client.get(f"/api/v1/conversations/{conversation['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_create_conversation_validates_agent(app_client: AsyncClient) -> None:
    response = await app_client.post("/api/v1/conversations", json={"agent_id": "01J8Z0000000000000000000ZZ"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_messages_are_paginated_by_cursor(app_client: AsyncClient, agent: dict) -> None:
    """3.1：`messages` 用游标分页（`meta.next_cursor`），默认倒序。"""
    conversation_id = (await app_client.post("/api/v1/conversations", json={"agent_id": agent["id"]})).json()["data"][
        "id"
    ]

    for index in range(3):
        response = await app_client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"content": f"第 {index} 问"}
        )
        assert response.status_code == 200
        await collect_sse(response.text)  # 消费完 SSE，确保 Run 落库

    first_page = await app_client.get(f"/api/v1/conversations/{conversation_id}/messages", params={"limit": 2})
    body = first_page.json()
    assert [row["role"] for row in body["data"]] == ["assistant", "user"]
    assert body["meta"]["total"] == 2
    cursor = body["meta"]["next_cursor"]
    assert cursor is not None

    second_page = await app_client.get(
        f"/api/v1/conversations/{conversation_id}/messages", params={"limit": 2, "cursor": cursor}
    )
    assert [row["seq"] for row in second_page.json()["data"]] == [4, 3]
    assert second_page.json()["meta"]["next_cursor"] is not None

    bad_cursor = await app_client.get(
        f"/api/v1/conversations/{conversation_id}/messages", params={"cursor": "not-a-seq"}
    )
    assert bad_cursor.status_code == 422


@pytest.mark.asyncio
async def test_run_is_rejected_for_disabled_agent(
    app_client: AsyncClient, db_session: AsyncSession, agent: dict
) -> None:
    """2.4：Agent 禁用后不接受新 Run（`AGENT_DISABLED`，409）。"""
    conversation_id = (await app_client.post("/api/v1/conversations", json={"agent_id": agent["id"]})).json()["data"][
        "id"
    ]
    await app_client.patch(f"/api/v1/agents/{agent['id']}", json={"status": "disabled"})

    response = await app_client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "你好"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_DISABLED"


@pytest.mark.asyncio
async def test_conversation_messages_404_for_unknown_conversation(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/conversations/01J8Z0000000000000000000CC/messages")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_404(app_client: AsyncClient) -> None:
    response = await app_client.post("/api/v1/runs/01J8Z0000000000000000000RR/cancel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_run_registry_guard_is_scoped_per_conversation() -> None:
    """1.5.4：同一会话只能有一个活跃 Run；不同会话互不影响。"""
    from app.core.errors import ConflictError

    registry = run_service.RunRegistry(max_concurrent=2)
    registry.begin(run_id="run-a", conversation_id="conv-1")
    with pytest.raises(ConflictError):
        registry.begin(run_id="run-b", conversation_id="conv-1")
    registry.begin(run_id="run-c", conversation_id="conv-2")  # 另一会话不受影响
    assert set(registry.active_run_ids()) == {"run-a", "run-c"}

    assert registry.request_cancel("run-a") is True
    registry.finish("run-a")
    assert registry.request_cancel("run-a") is False  # 已不在活跃表里
    assert set(registry.active_run_ids()) == {"run-c"}
