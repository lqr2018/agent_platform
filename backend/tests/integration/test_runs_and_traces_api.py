"""Run / Trace 只读 API（详细设计 3.2.4 / 3.2.9 / 4.8）。

先跑一轮真实的 SSE 对话（fake provider），再断言 Run 与 Trace 的读取接口与取消语义。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import collect_sse, create_agent, create_conversation, create_fake_provider


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


@pytest_asyncio.fixture
async def finished_run(app_client: AsyncClient, db_session: AsyncSession) -> dict:
    """跑一轮对话，返回 `{run, trace_id, conversation_id}`。"""
    provider = await create_fake_provider(db_session, name="trace-provider")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="trace-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    response = await app_client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "你好"})
    events = await collect_sse(response.text)
    run_id = dict(events)["run.started"]["run_id"]
    trace_id = dict(events)["run.started"]["trace_id"]
    return {"run_id": run_id, "trace_id": trace_id, "conversation_id": conversation_id}


@pytest.mark.asyncio
async def test_run_list_and_detail(app_client: AsyncClient, finished_run: dict) -> None:
    listing = await app_client.get("/api/v1/runs", params={"conversation_id": finished_run["conversation_id"]})
    body = listing.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["id"] == finished_run["run_id"]
    assert body["data"][0]["status"] == "succeeded"

    detail = await app_client.get(f"/api/v1/runs/{finished_run['run_id']}")
    data = detail.json()["data"]
    assert data["steps"] == 1
    assert data["total_tokens"] == 15
    assert data["tool_call_count"] == 0
    assert data["input"]["text"] == "你好"
    assert data["output"]["finish_reason"] == "stop"
    assert data["trace_id"] == finished_run["trace_id"]
    assert data["latency_ms"] is not None


@pytest.mark.asyncio
async def test_cancel_finished_run_conflicts(app_client: AsyncClient, finished_run: dict) -> None:
    """2.6：进入终态后禁止再次变更（`RUN_ALREADY_FINISHED`，409）。"""
    response = await app_client.post(f"/api/v1/runs/{finished_run['run_id']}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ALREADY_FINISHED"


@pytest.mark.asyncio
async def test_trace_list_detail_and_span(app_client: AsyncClient, finished_run: dict) -> None:
    listing = await app_client.get("/api/v1/traces", params={"kind": "chat"})
    body = listing.json()
    assert body["data"][0]["id"] == finished_run["trace_id"]
    assert body["data"][0]["span_count"] == 3
    assert body["data"][0]["status"] == "succeeded"

    detail = await app_client.get(f"/api/v1/traces/{finished_run['trace_id']}")
    trace_detail = detail.json()["data"]
    assert trace_detail["trace"]["id"] == finished_run["trace_id"]
    assert [span["span_type"] for span in trace_detail["spans"]] == ["run", "agent", "llm"]
    assert [span["seq"] for span in trace_detail["spans"]] == [1, 2, 3]
    llm_span = next(span for span in trace_detail["spans"] if span["span_type"] == "llm")
    assert "input" not in llm_span  # 列表用 SpanSummary，不含 input/output（3.2.9）

    span_detail = await app_client.get(f"/api/v1/spans/{llm_span['id']}")
    span_data = span_detail.json()["data"]
    assert span_data["attributes"]["model"] == "fake-model"
    assert span_data["output"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_trace_unknown_ids_return_404(app_client: AsyncClient) -> None:
    assert (await app_client.get("/api/v1/traces/ffffffffffffffffffffffffffffffff")).status_code == 404
    assert (await app_client.get("/api/v1/spans/ffffffffffffffff")).status_code == 404


@pytest.mark.asyncio
async def test_trace_cursor_validation(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/traces", params={"cursor": "not-a-cursor"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_trace_list_is_keyset_paginated(
    app_client: AsyncClient, finished_run: dict, db_session: AsyncSession
) -> None:
    """3.2.9：Trace 列表用游标分页；第二页返回 `next_cursor=None`。"""
    provider = await create_fake_provider(db_session, name="page-provider")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="page-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))
    await collect_sse(
        (await app_client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "第二问"})).text
    )

    first = await app_client.get("/api/v1/traces", params={"limit": 1})
    first_body = first.json()
    assert len(first_body["data"]) == 1
    assert first_body["meta"]["next_cursor"] is not None

    second = await app_client.get("/api/v1/traces", params={"limit": 10, "cursor": first_body["meta"]["next_cursor"]})
    second_body = second.json()
    assert len(second_body["data"]) == 1
    assert second_body["data"][0]["id"] != first_body["data"][0]["id"]
    assert second_body["meta"]["next_cursor"] is None
