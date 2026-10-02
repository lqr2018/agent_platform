"""Chat SSE 端到端测试（详细设计 7.2 的集成要求 + 3.4 事件契约）。

链路：建 fake Provider → 建 Agent → 建会话 → `POST /conversations/{id}/messages` →
断言 SSE 事件序列、`messages` 行数、`runs.status`、`traces` 三个 span（run → agent → llm）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, Run, Span, Trace
from tests.helpers import collect_sse, create_agent, create_conversation, create_fake_provider


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


async def _post_message(client: AsyncClient, conversation_id: str, content: str) -> list[tuple[str, dict]]:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content, "stream": True},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return await collect_sse(response.text)


@pytest.mark.asyncio
async def test_chat_stream_persists_messages_run_and_trace(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """正常路径：事件序列 + 落库（messages 2 行 / run succeeded / trace 3 span）。"""
    provider = await create_fake_provider(db_session)
    agent = await create_agent(app_client, provider_id=str(provider.id), name="chat-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    events = await _post_message(app_client, conversation_id, "你好")
    names = [name for name, _ in events]

    assert names[0] == "run.started"
    assert names[-1] == "done"
    assert "message.started" in names
    assert names.count("message.delta") >= 2  # 流式分片
    assert "message.completed" in names
    assert "usage.updated" in names
    assert names[-2] == "run.completed"

    started = events[0][1]
    assert started["conversation_id"] == conversation_id
    completed = dict(events)["run.completed"]
    assert completed["status"] == "succeeded" and completed["steps"] == 1

    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert run.status == "succeeded"
    assert run.trace_id == started["trace_id"]
    assert run.total_tokens == 15  # fake provider 固定 10 + 5

    messages = list(
        (
            await db_session.execute(
                select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].content.startswith("FAKE_RESPONSE: 你好")
    assert messages[1].run_id == run.id
    assert messages[1].finish_reason == "stop"

    spans = list(
        (await db_session.execute(select(Span).where(Span.trace_id == run.trace_id).order_by(Span.seq))).scalars().all()
    )
    assert [span.span_type for span in spans] == ["run", "agent", "llm"]  # 4.8.1 的固定层级
    assert all(span.latency_ms is not None for span in spans)
    assert spans[2].prompt_tokens == 10 and spans[2].completion_tokens == 5

    trace = await db_session.get(Trace, run.trace_id)
    assert trace is not None and trace.status == "succeeded" and trace.span_count == 3
    assert trace.total_tokens == 15

    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    assert conversation.message_count == 2
    assert conversation.title == "你好"  # 标题取首条用户消息前 30 字（2.6）


@pytest.mark.asyncio
async def test_chat_model_error_marks_run_failed(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """错误路径：`simulate_error=timeout` → `MODEL_TIMEOUT` + `run.failed`（附录 A）。"""
    provider = await create_fake_provider(db_session, name="fake-provider-err")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="error-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    events = await _post_message(app_client, conversation_id, "simulate_error=timeout")
    names = [name for name, _ in events]
    assert "run.failed" in names
    assert "run.completed" not in names
    assert dict(events)["run.failed"]["error_code"] == "MODEL_TIMEOUT"

    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert run.status == "failed" and run.error_code == "MODEL_TIMEOUT"

    trace = await db_session.get(Trace, run.trace_id)
    assert trace is not None and trace.status == "failed"
    failed_spans = list(
        (await db_session.execute(select(Span).where(Span.trace_id == run.trace_id, Span.status == "error")))
        .scalars()
        .all()
    )
    assert failed_spans  # 4.8.2：失败也要留痕


@pytest.mark.asyncio
async def test_message_started_id_matches_persisted_row(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """`message.started` 的 id 与库里那行一致（3.4 事件 3 的稳定 id）。"""
    provider = await create_fake_provider(db_session, name="fake-provider-ids")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="ids-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    events = await _post_message(app_client, conversation_id, "数一下")
    started = dict(events)["message.started"]
    delta = dict(events)["message.delta"]
    completed = dict(events)["message.completed"]
    assert started["message_id"] == delta["message_id"] == completed["message_id"]

    message = await db_session.get(Message, started["message_id"])
    assert message is not None and message.role == "assistant"
    assert (await db_session.execute(select(func.count(Message.id)))).scalar_one() == 2


@pytest.mark.asyncio
async def test_conversation_guard_blocks_parallel_runs(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """1.5.4：同一会话最多 1 个活跃 Run（第二个请求 409）。"""
    from app.services import run_service

    provider = await create_fake_provider(db_session, name="fake-provider-lock")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="lock-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    registry = run_service.get_run_registry()
    registry.begin(run_id="01TESTRUN00000000000000000", conversation_id=conversation_id)
    try:
        response = await app_client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"content": "hi", "stream": True}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"
    finally:
        registry.finish("01TESTRUN00000000000000000")
