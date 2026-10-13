"""`GET /stats/overview`（详细设计 3.2.9 / 4.8.3 / 7.8 任务 5）。

两类证据：
- **真实链路**：跑一轮对话（fake provider）后，指标必须反映这一轮；
- **直接造行**：固定 2 条收敛 + 1 条在跑的 Run 与 4 个 tool span（2 ok / 2 error），
  钉住公式（成功率 / 错误率 / 均值只算收敛行）与 4.9.2 报告共用的字段名。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunKind, RunStatus, SpanStatus, SpanType
from app.db.base import now_utc
from app.db.models import Run, Span, Trace
from tests.helpers import collect_sse, create_agent, create_conversation, create_fake_provider

TRACE_ID = "a" * 32


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


async def _seed_runs_and_tool_spans(session: AsyncSession) -> None:
    """造 3 条 Run（succeeded / failed / running）+ 同一 trace 上的 4 个 tool span。"""
    started = now_utc()
    session.add(
        Trace(
            id=TRACE_ID,
            name="seeded",
            kind=str(RunKind.CHAT),
            status=str(RunStatus.SUCCEEDED),
            span_count=4,
            started_at=started,
            ended_at=started,
        )
    )
    session.add_all(
        [
            Run(
                id="run-seeded-ok",
                kind=str(RunKind.CHAT),
                status=str(RunStatus.SUCCEEDED),
                latency_ms=100,
                tool_call_count=1,
                total_tokens=10,
                total_cost_usd=Decimal("0.001"),
                started_at=started,
                ended_at=started,
                trace_id=TRACE_ID,
            ),
            Run(
                id="run-seeded-failed",
                kind=str(RunKind.CHAT),
                status=str(RunStatus.FAILED),
                latency_ms=200,
                tool_call_count=2,
                total_tokens=20,
                total_cost_usd=Decimal("0.002"),
                error_code="MODEL_TIMEOUT",
                started_at=started,
                ended_at=started,
                trace_id=TRACE_ID,
            ),
            # 在跑的 Run：不进比率分母，也不进均值（口径写在 stats_service 的模块 docstring）
            Run(
                id="run-seeded-running",
                kind=str(RunKind.CHAT),
                status=str(RunStatus.RUNNING),
                started_at=started,
                trace_id=TRACE_ID,
            ),
        ]
    )
    session.add_all(
        [
            Span(
                id=f"span-tool-{index}",
                trace_id=TRACE_ID,
                run_id="run-seeded-ok",
                span_type=str(SpanType.TOOL),
                name=f"tool:{index}",
                status=status,
                attributes={},
                seq=index,
                started_at=started,
                ended_at=started,
            )
            for index, status in enumerate(
                [str(SpanStatus.OK), str(SpanStatus.OK), str(SpanStatus.ERROR), str(SpanStatus.ERROR)], start=1
            )
        ]
    )
    await session.commit()


async def test_overview_reflects_a_real_chat_run(app_client: AsyncClient, db_session: AsyncSession) -> None:
    provider = await create_fake_provider(db_session, name="stats-provider")
    agent = await create_agent(app_client, provider_id=str(provider.id), name="stats-agent")
    conversation_id = await create_conversation(app_client, agent_id=str(agent["id"]))
    await collect_sse(
        (await app_client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "你好"})).text
    )

    response = await app_client.get("/api/v1/stats/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["request_id"]
    data = body["data"]

    assert data["total_runs"] == 1
    assert (data["succeeded"], data["failed"], data["canceled"], data["running"]) == (1, 0, 0, 0)
    assert data["terminal_runs"] == 1
    assert data["success_rate"] == 1.0
    assert data["error_rate"] == 0.0
    assert data["avg_latency_ms"] is not None
    assert data["avg_tool_calls"] == 0.0
    assert data["avg_total_tokens"] == 15.0  # fake provider：prompt 10 + completion 5
    assert data["total_tokens"] == 15
    assert data["total_cost_usd"] == 0.0
    # `FakeLLMProvider` 自带 `cost_usd=Decimal(0)`（`runtime/llm/fake.py`，只有
    # `OpenAICompatibleProvider` 按 `models[]` 价格估算，4.1.2 第 7 条）；
    # 成本聚合本身的正例在下一个用例（造行）里钉。
    assert data["tool_spans_total"] == 0
    assert data["tool_success_rate"] is None  # 无 tool span → null（不是 0.0）
    assert data["kind"] is None and data["from_at"] is None and data["to_at"] is None
    assert data["generated_at"].endswith("Z")


async def test_overview_aggregates_seeded_runs_and_tool_spans(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_runs_and_tool_spans(db_session)

    data = (await app_client.get("/api/v1/stats/overview")).json()["data"]
    assert data["total_runs"] == 3
    assert (data["succeeded"], data["failed"], data["canceled"], data["running"]) == (1, 1, 0, 1)
    assert data["terminal_runs"] == 2
    assert data["success_rate"] == 0.5
    assert data["error_rate"] == 0.5
    assert data["avg_latency_ms"] == 150.0  # (100 + 200) / 2，在跑的那条不进均值
    assert data["avg_tool_calls"] == 1.5
    assert data["avg_total_tokens"] == 15.0
    assert data["total_tokens"] == 30
    assert data["total_cost_usd"] == 0.003
    assert (data["tool_spans_total"], data["tool_spans_succeeded"]) == (4, 2)
    assert data["tool_success_rate"] == 0.5


async def test_overview_window_and_kind_filters(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """空窗口返回 0 与 `null`（不抛错）；`?kind=` 用来把评测批次排除在平台指标之外。"""
    await _seed_runs_and_tool_spans(db_session)
    started = datetime.now(UTC)

    empty = (
        await app_client.get("/api/v1/stats/overview", params={"from": (started + timedelta(days=1)).isoformat()})
    ).json()["data"]
    assert empty["total_runs"] == 0
    assert empty["terminal_runs"] == 0
    assert (empty["success_rate"], empty["error_rate"]) == (None, None)
    assert (empty["avg_latency_ms"], empty["avg_tool_calls"], empty["avg_total_tokens"]) == (None, None, None)
    assert empty["total_tokens"] == 0 and empty["total_cost_usd"] == 0.0
    assert empty["tool_spans_total"] == 0 and empty["tool_success_rate"] is None
    assert empty["from_at"] is not None and empty["to_at"] is None

    windowed = (
        await app_client.get(
            "/api/v1/stats/overview",
            params={
                "from": (started - timedelta(minutes=5)).isoformat(),
                "to": (started + timedelta(minutes=5)).isoformat(),
            },
        )
    ).json()["data"]
    assert windowed["total_runs"] == 3

    eval_only = (await app_client.get("/api/v1/stats/overview", params={"kind": "eval"})).json()["data"]
    assert eval_only["total_runs"] == 0 and eval_only["kind"] == "eval"

    chat_only = (await app_client.get("/api/v1/stats/overview", params={"kind": "chat"})).json()["data"]
    assert chat_only["total_runs"] == 3 and chat_only["success_rate"] == 0.5


async def test_overview_rejects_a_malformed_window(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/stats/overview", params={"from": "not-a-date"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
