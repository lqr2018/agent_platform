"""Chat 工具调用端到端测试（详细设计 7.3 的集成要求 + 3.4 事件 2/6/7/15）。

链路：fake Provider 的 `calculator` 脚本 → 一轮工具调用 →
断言 SSE 事件、`messages` 序列（user → assistant(tool_calls) → tool → assistant(stop)）、
`tool_invocations` 一行、`runs.tool_call_count` 与 tool span。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Run, Span, ToolInvocation
from app.runtime.tools.registry import builtin_tool_id
from tests.helpers import collect_sse, create_agent, create_conversation, create_fake_provider

CALCULATE_PROMPT = "帮我计算 2+2"
CALCULATOR_ID = builtin_tool_id("calculator")


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
    return await collect_sse(response.text)


async def _messages(session: AsyncSession, conversation_id: str) -> list[Message]:
    rows = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_calculator_tool_call_is_persisted_end_to_end(app_client: AsyncClient, db_session: AsyncSession) -> None:
    provider = await create_fake_provider(db_session, name="fake-provider-tools")
    agent = await create_agent(
        app_client,
        provider_id=str(provider.id),
        name="tool-agent",
        tool_ids=[CALCULATOR_ID],
    )
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    events = await _post_message(app_client, conversation_id, CALCULATE_PROMPT)
    payloads = dict(events)
    names = [name for name, _ in events]

    assert names[0] == "run.started" and names[-1] == "done"
    assert names.count("agent.step.started") == 2  # 一次工具往返 = 两步（4.4.3）
    assert "tool.call.started" in names and "tool.call.completed" in names
    assert payloads["tool.call.started"]["tool_name"] == "calculator"
    assert payloads["tool.call.completed"]["result_preview"] == "2+2 = 4"
    assert payloads["run.completed"]["tool_call_count"] == 1
    assert payloads["run.completed"]["steps"] == 2
    assert [item["has_tool_calls"] for name, item in events if name == "agent.step.completed"] == [True, False]

    messages = await _messages(db_session, conversation_id)
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[0].meta["tools"] == [CALCULATOR_ID]  # chat_service 写入 messages.meta（2.6）
    assert messages[1].tool_calls == [{"id": "call_1", "name": "calculator", "arguments": {"expression": "2+2"}}]
    assert messages[1].finish_reason == "tool_calls"
    assert (messages[2].tool_call_id, messages[2].name) == ("call_1", "calculator")
    assert messages[2].content == "2+2 = 4"
    assert messages[3].content.startswith("结果：")  # fake provider 第二轮（4.1.3）

    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert (run.status, run.steps, run.tool_call_count) == ("succeeded", 2, 1)

    invocation = (await db_session.execute(select(ToolInvocation))).scalar_one()
    assert invocation.run_id == run.id and invocation.trace_id == run.trace_id
    assert (invocation.tool_id, invocation.tool_name) == (CALCULATOR_ID, "calculator")
    assert invocation.arguments == {"expression": "2+2"}
    assert (invocation.status, invocation.permission_decision) == ("succeeded", "allow")
    assert invocation.result == "2+2 = 4" and invocation.result_truncated is False
    assert (invocation.step_index, invocation.call_index) == (1, 1)
    assert invocation.approval_id is None  # SD-17

    tool_spans = list(
        (await db_session.execute(select(Span).where(Span.trace_id == run.trace_id, Span.span_type == "tool")))
        .scalars()
        .all()
    )
    assert len(tool_spans) == 1
    assert tool_spans[0].id == invocation.span_id
    assert tool_spans[0].input == {"arguments": {"expression": "2+2"}}

    listed = await app_client.get("/api/v1/tool-invocations", params={"run_id": run.id})
    assert listed.status_code == 200
    assert [item["tool_name"] for item in listed.json()["data"]] == ["calculator"]


@pytest.mark.asyncio
async def test_agent_without_tools_keeps_phase1_behaviour(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """7.3 回归：无工具 Agent 不下发 tools、不产生 tool 事件与调用明细（Phase 1 行为不变）。"""
    provider = await create_fake_provider(db_session, name="fake-provider-no-tools")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="plain-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))

    events = await _post_message(app_client, conversation_id, CALCULATE_PROMPT)
    names = [name for name, _ in events]

    assert "tool.call.started" not in names and "tool.call.completed" not in names
    assert dict(events)["run.completed"]["tool_call_count"] == 0
    assert dict(events)["message.completed"]["content"].startswith("FAKE_RESPONSE")

    messages = await _messages(db_session, conversation_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert (await db_session.execute(select(ToolInvocation))).scalars().all() == []
