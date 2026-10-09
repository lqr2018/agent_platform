"""Workflow 运行端到端集成测试（详细设计 7.4 / 3.2.7 / 4.5.3 / 4.5.4）。

真实 `create_app()` + lifespan + 临时 SQLite（9.1）：验证一条完整链路 ——
建图 → 发布 → 启动运行（202）→ 轮询 `node-runs` → 状态/落库/Trace → resume / cancel。
Agent 节点走 `FakeLLMProvider`（4.1.3），`tool` 节点走内置 `calculator`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Run, WorkflowRun
from app.runtime.workflow import NodeOutcome
from app.runtime.workflow import nodes as nodes_module
from app.services import run_service, workflow_service
from tests.helpers import FAKE_MODEL, collect_sse, create_agent, create_conversation, create_fake_provider
from tests.integration.test_workflows_api import create_workflow, definition_with, with_agent_ids

TERMINAL = {"succeeded", "failed", "canceled"}


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture。"""
    return session


async def wait_for_terminal(client: AsyncClient, run_id: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """轮询运行详情直到终态（3.4：Workflow 运行不返回 SSE，前端就是这么做的）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/v1/workflow-runs/{run_id}")
        assert response.status_code == 200, response.text
        data = dict(response.json()["data"])
        if data["status"] in TERMINAL:
            return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow run {run_id} did not finish in {timeout}s")


async def node_runs_of(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/workflow-runs/{run_id}/node-runs")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["data"]]


async def start_workflow_run(client: AsyncClient, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": payload})
    assert response.status_code == 202, response.text
    return dict(response.json()["data"])


def slow_node_execution(delay: float) -> Any:
    """节点执行的测试替身：睡 `delay` 秒（制造"超时 / 取消落在节点执行中"的窗口）。

    `SimpleEngine` 是通过 `nodes.execute_node` 这个**模块属性**调用节点的 —— monkeypatch 它即可
    在不碰生产代码的前提下把节点变慢（9.2 的测试替身规则）。
    """

    async def _execute(*_args: Any, **_kwargs: Any) -> NodeOutcome:
        await asyncio.sleep(delay)
        return NodeOutcome(state_updates={"output": "slow"}, output_summary={"stub": True})

    return _execute


async def wait_for_running_node(client: AsyncClient, run_id: str, *, timeout: float = 10.0) -> None:
    """等到该 Run 出现一行 `running` 的节点（"节点真的开始跑了"的同步点）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if any(row["status"] == "running" for row in await node_runs_of(client, run_id)):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} never had a running node")


async def api_run_of(session: AsyncSession, workflow_run_id: str) -> Run:
    """取该 WorkflowRun 的 `runs` 行（`canceled_at` 这类字段只能从库里断言，API 不返回）。

    先 `rollback()`：结束本 session 上任何隐式的只读事务，避免读到取消写入之前的快照。
    """
    await session.rollback()
    statement = select(Run).where(Run.workflow_run_id == workflow_run_id).order_by(Run.started_at.desc())
    api_run = (await session.execute(statement)).scalars().first()
    assert api_run is not None, f"workflow run {workflow_run_id} has no runs row"
    return api_run


async def test_manual_run_end_to_end(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """DoD：JSON 定义的图**不改代码**即可跑通，`node_runs` 记录每个节点的状态/耗时/输出摘要。"""
    provider = await create_fake_provider(db_session, name="e2e-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="e2e-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="e2e-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="e2e")
    published = await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    assert published.status_code == 200

    started = await start_workflow_run(app_client, workflow["id"], {"input": "1+1", "expr": "2*3"})
    assert started["status"] == "running"
    assert started["workflow_version"] == 2
    assert started["trigger"] == "api"

    run = await wait_for_terminal(app_client, started["id"])
    assert run["status"] == "succeeded", run
    assert run["current_node_id"] == "end"
    assert run["state"]["plan"] == "FAKE_RESPONSE: 1+1"
    assert run["state"]["result"] == "2*3 = 6"
    assert run["state"]["draft"] == "FAKE_RESPONSE: 补充说明：2*3 = 6"
    assert run["state"]["nodes"]["calc"]["output"] == "2*3 = 6"
    assert run["trace_id"]

    rows = await node_runs_of(app_client, started["id"])
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert [row["node_id"] for row in rows] == ["start", "planner", "calc", "router", "writer", "end"]
    assert {row["status"] for row in rows} == {"succeeded"}
    assert {row["iteration"] for row in rows} == {1}
    assert {row["attempt"] for row in rows} == {1}
    assert rows[1]["input"] == {"text": "1+1"}
    assert rows[1]["output"]["agent_name"] == "e2e-planner"
    assert rows[2]["input"] == {"arguments": {"expression": "2*3"}}
    assert rows[2]["output"]["tool_name"] == "calculator"
    assert rows[3]["output"]["matched"] == "state.expr != ''"
    assert all(row["latency_ms"] is not None for row in rows)
    assert all(row["span_id"] for row in rows)

    # `runs` 行（4.8.1 的 trace 根）与 Trace 树（node:{id} 命名，DoD 4）
    api_runs = (await app_client.get("/api/v1/runs", params={"kind": "workflow"})).json()["data"]
    assert len(api_runs) == 1
    api_run = api_runs[0]
    assert api_run["workflow_run_id"] == started["id"]
    assert api_run["status"] == "succeeded"
    assert api_run["tool_call_count"] == 1
    assert rows[1]["agent_run_id"] == api_run["id"]
    assert rows[2]["agent_run_id"] is None

    spans = (await app_client.get(f"/api/v1/traces/{run['trace_id']}")).json()["data"]["spans"]
    names = [span["name"] for span in spans]
    assert "workflow:e2e" in names
    assert "node:planner" in names and "node:calc" in names
    assert {"run", "workflow", "node", "agent", "tool", "llm"} <= {span["span_type"] for span in spans}

    # 运行列表（3.2.7 的 `?workflow_id=&status=`）
    listing = await app_client.get("/api/v1/workflow-runs", params={"workflow_id": workflow["id"]})
    assert [item["id"] for item in listing.json()["data"]] == [started["id"]]


async def test_condition_default_branch_and_tool_continue(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """4.5.2：`tool` 失败默认 `continue`（错误文本进 state）；`condition` 不命中走 `default_next`。"""
    provider = await create_fake_provider(db_session, name="branch-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="branch-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="branch-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="branch")

    started = await start_workflow_run(app_client, workflow["id"], {"input": "hi", "expr": ""})
    run = await wait_for_terminal(app_client, started["id"])

    assert run["status"] == "succeeded"
    rows = await node_runs_of(app_client, started["id"])
    assert [row["node_id"] for row in rows] == ["start", "planner", "calc", "router", "end"]
    calc_row = next(row for row in rows if row["node_id"] == "calc")
    assert calc_row["status"] == "failed"
    assert calc_row["error_code"] == "TOOL_INVALID_ARGUMENTS"
    assert run["state"]["result"].startswith("[TOOL_INVALID_ARGUMENTS]")
    assert run["state"]["nodes"]["calc"]["error_code"] == "TOOL_INVALID_ARGUMENTS"
    assert "draft" not in run["state"]


async def _rewrite_checkpoint_input(db_session: AsyncSession, run_id: str, text: str) -> None:
    """把断点里的输入换成"触发源已消失"的文本（等价于：限流过去了 / 上游恢复了）。"""
    workflow_run = await db_session.get(WorkflowRun, run_id)
    assert workflow_run is not None
    state = dict(workflow_run.state or {})
    state["input"] = text
    checkpoint = dict(workflow_run.checkpoint or {})
    checkpoint["state"] = state
    workflow_run.state = state
    workflow_run.checkpoint = checkpoint
    await db_session.commit()


async def test_resume_from_retryable_failure(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """4.5.4 场景 1：`failed` + 可重试错误 → `resume` 从该节点续跑，且不回放前面的节点。"""
    provider = await create_fake_provider(db_session, name="resume-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="resume-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="resume-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="resume")

    started = await start_workflow_run(app_client, workflow["id"], {"input": "simulate_error=timeout", "expr": ""})
    failed = await wait_for_terminal(app_client, started["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "MODEL_TIMEOUT"
    assert failed["current_node_id"] == "planner"
    first_attempt = await node_runs_of(app_client, started["id"])
    assert [row["node_id"] for row in first_attempt] == ["start", "planner"]
    assert first_attempt[-1]["error_code"] == "MODEL_TIMEOUT"
    assert first_attempt[-1]["status"] == "failed"

    await _rewrite_checkpoint_input(db_session, started["id"], "1+1")
    resumed = await app_client.post(f"/api/v1/workflow-runs/{started['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["status"] == "running"

    finished = await wait_for_terminal(app_client, started["id"])
    assert finished["status"] == "succeeded", finished
    assert finished["state"]["plan"] == "FAKE_RESPONSE: 1+1"

    rows = await node_runs_of(app_client, started["id"])
    # `seq` 跨尝试连续（3.2.7 的排序契约），且 attempt 1 的失败行保留在案
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    planner_rows = [row for row in rows if row["node_id"] == "planner"]
    assert [row["status"] for row in planner_rows] == ["failed", "succeeded"]
    assert [row["node_id"] for row in rows].count("start") == 1
    assert [row["node_id"] for row in rows][2:] == ["planner", "calc", "router", "end"]


async def test_resume_rejects_non_retryable_failure(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """4.5.4：失败原因不在可重试集合里 → 409 `WORKFLOW_RUN_NOT_RESUMABLE`。"""
    provider = await create_fake_provider(db_session, name="fatal-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="fatal-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="fatal-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    definition = with_agent_ids(definition_with(), agents)
    calc_node = next(node for node in definition["nodes"] if node["id"] == "calc")
    calc_node["on_error"] = "fail"
    workflow = await create_workflow(app_client, agents, name="fatal", definition=definition)

    started = await start_workflow_run(app_client, workflow["id"], {"input": "hi", "expr": ""})
    failed = await wait_for_terminal(app_client, started["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "TOOL_INVALID_ARGUMENTS"

    response = await app_client.post(f"/api/v1/workflow-runs/{started['id']}/resume")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKFLOW_RUN_NOT_RESUMABLE"
    assert response.json()["error"]["details"]["reason"] == "ERROR_NOT_RETRYABLE"


async def test_resume_and_cancel_finished_run_conflict(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """终态运行既不能 resume 也不能 cancel（2.6：进入终态后禁止再次变更）。"""
    provider = await create_fake_provider(db_session, name="done-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="done-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="done-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="done")
    started = await start_workflow_run(app_client, workflow["id"], {"input": "1+1", "expr": "1"})
    assert (await wait_for_terminal(app_client, started["id"]))["status"] == "succeeded"

    resume = await app_client.post(f"/api/v1/workflow-runs/{started['id']}/resume")
    assert resume.status_code == 409
    assert resume.json()["error"]["details"]["reason"] == "RUN_ALREADY_FINISHED"

    cancel = await app_client.post(f"/api/v1/workflow-runs/{started['id']}/cancel")
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "RUN_ALREADY_FINISHED"


async def test_cancel_orphan_run_marks_both_rows_canceled(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """孤儿 Run（进程重启后仍 `running`）取消：直接落终态（3.2.7 / 2.6）。"""
    provider = await create_fake_provider(db_session, name="cancel-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="cancel-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="cancel-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="cancel")
    workflow_run = WorkflowRun(
        workflow_id=workflow["id"],
        workflow_version=1,
        definition_snapshot=workflow["definition"],
        status="running",
        trigger="manual",
        input={"input": "x", "expr": ""},
        output={},
        state={"input": "x", "expr": "", "nodes": {}, "run": {}},
        current_node_id="planner",
        checkpoint={},
    )
    db_session.add(workflow_run)
    await db_session.commit()
    await db_session.refresh(workflow_run)
    api_run = Run(
        id="01J8Z000000000000000000AR1",
        kind="workflow",
        workflow_run_id=workflow_run.id,
        status="running",
        input={},
        output={},
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(api_run)
    await db_session.commit()

    response = await app_client.post(f"/api/v1/workflow-runs/{workflow_run.id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "canceled"

    await db_session.refresh(api_run)
    assert api_run.status == "canceled"
    assert api_run.error_code == "RUN_CANCELED"

    detail = (await app_client.get(f"/api/v1/workflow-runs/{workflow_run.id}")).json()["data"]
    assert detail["error_code"] == "RUN_CANCELED"
    assert detail["ended_at"] is not None


async def test_cancel_active_run_only_signals(db_session: AsyncSession) -> None:
    """活跃 Run 的取消只置信号，由运行中的任务收敛终态（与 `run_service.cancel_run` 同语义）。"""
    registry = run_service.RunRegistry(max_concurrent=1)
    workflow_run = WorkflowRun(
        workflow_id="01J8Z000000000000000000WF1",
        workflow_version=1,
        definition_snapshot={},
        status="running",
        trigger="manual",
        input={},
        output={},
        state={},
        checkpoint={},
    )
    db_session.add(workflow_run)
    await db_session.commit()
    await db_session.refresh(workflow_run)
    api_run_id = "01J8Z000000000000000000AR2"
    db_session.add(
        Run(
            id=api_run_id,
            kind="workflow",
            workflow_run_id=workflow_run.id,
            status="running",
            input={},
            output={},
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db_session.commit()
    cancel_event = registry.begin(run_id=api_run_id, conversation_id=None)

    settings = get_settings()
    row = await workflow_service.cancel_run(db_session, workflow_run.id, settings=settings, registry=registry)

    assert cancel_event.is_set()
    assert registry.is_active(api_run_id)
    assert row.status == "running"  # 终态由运行中的任务收敛
    registry.finish(api_run_id)


async def test_generic_run_cancel_delegates_to_workflow_run(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """3.1：取消有两个入口 —— `/runs/{id}/cancel`（通用）对 `kind=workflow` 的行必须与
    `/workflow-runs/{id}/cancel` 行为一致（两行一起收敛）。"""
    provider = await create_fake_provider(db_session, name="delegate-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="delegate-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="delegate-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="delegate")
    workflow_run = WorkflowRun(
        workflow_id=workflow["id"],
        workflow_version=1,
        definition_snapshot=workflow["definition"],
        status="running",
        trigger="manual",
        input={},
        output={},
        state={},
        checkpoint={},
    )
    db_session.add(workflow_run)
    await db_session.commit()
    await db_session.refresh(workflow_run)
    api_run_id = "01J8Z000000000000000000AR4"
    db_session.add(
        Run(
            id=api_run_id,
            kind="workflow",
            workflow_run_id=workflow_run.id,
            status="running",
            input={},
            output={},
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db_session.commit()

    response = await app_client.post(f"/api/v1/runs/{api_run_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "canceled"

    await db_session.refresh(workflow_run)
    assert workflow_run.status == "canceled"
    assert workflow_run.error_code == "RUN_CANCELED"


async def test_orphan_convergence_marks_stale_running_rows(
    app_client: AsyncClient, db_session: AsyncSession, tmp_sqlite: Any
) -> None:
    """6.3 第 3 条：超 `timeout × 2` 的 `running` 行在启动自检里被标 `failed` / `RUN_ABANDONED`。"""
    provider = await create_fake_provider(db_session, name="orphan-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="orphan-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="orphan-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="orphan")

    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10_000)
    workflow_run = WorkflowRun(
        workflow_id=workflow["id"],
        workflow_version=1,
        definition_snapshot=workflow["definition"],
        status="running",
        trigger="manual",
        input={},
        output={},
        state={},
        checkpoint={},
        started_at=stale,
    )
    db_session.add(workflow_run)
    await db_session.commit()
    await db_session.refresh(workflow_run)
    db_session.add(
        Run(
            id="01J8Z000000000000000000AR3",
            kind="workflow",
            workflow_run_id=workflow_run.id,
            status="running",
            input={},
            output={},
            started_at=stale,
        )
    )
    await db_session.commit()

    counts = await workflow_service.converge_orphan_runs(db_session, get_settings())
    assert counts == {"workflow_runs": 1, "runs": 1}

    await db_session.refresh(workflow_run)
    assert workflow_run.status == "failed"
    assert workflow_run.error_code == "RUN_ABANDONED"
    detail = (await app_client.get(f"/api/v1/workflow-runs/{workflow_run.id}")).json()["data"]
    assert detail["error_code"] == "RUN_ABANDONED"
    assert detail["ended_at"] is not None


async def test_agent_binding_requires_published_workflow(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """2.9：Agent 只能绑定**已发布**的 Workflow（draft 只用于试跑，避免行为随编辑漂移）。"""
    provider = await create_fake_provider(db_session, name="bind-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="bind-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="bind-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="bind")
    body = {
        "name": "bound-agent",
        "model_provider_id": str(provider.id),
        "model_name": FAKE_MODEL,
        "workflow_id": workflow["id"],
    }

    draft_bind = await app_client.post("/api/v1/agents", json=body)
    assert draft_bind.status_code == 422, draft_bind.text
    assert draft_bind.json()["error"]["details"] == {"field": "workflow_id", "status": "draft"}

    assert (await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")).status_code == 200
    bound = await app_client.post("/api/v1/agents", json=body)
    assert bound.status_code == 201, bound.text
    assert bound.json()["data"]["workflow_id"] == workflow["id"]

    unknown = await app_client.post(
        "/api/v1/agents", json={**body, "name": "bound-agent-2", "workflow_id": "01J8Z0000000000000000000W9"}
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["field"] == "workflow_id"


async def test_chat_inline_workflow_streams_node_events(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """4.4.4 + 3.4 事件 13/14：Agent 绑定 Workflow 后，Chat 一轮对话由引擎驱动（同一条 SSE）。"""
    provider = await create_fake_provider(db_session, name="inline-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="inline-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="inline-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    definition = {
        "name": "inline",
        "nodes": [
            {"id": "start", "type": "start", "next": "planner"},
            {
                "id": "planner",
                "type": "agent",
                "agent_id": agents["planner"],
                "input_template": "{{state.input}}",
                "output_key": "plan",
                "next": "writer",
            },
            {
                "id": "writer",
                "type": "agent",
                "agent_id": agents["writer"],
                "input_template": "润色：{{state.plan}}",
                "output_key": "draft",
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
        "config": {"max_steps": 6, "recursion_limit": 3, "timeout_seconds": 60},
    }
    workflow = await create_workflow(app_client, agents, name="inline", definition=definition)
    assert (await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")).status_code == 200

    bound = await app_client.post(
        "/api/v1/agents",
        json={
            "name": "inline-agent",
            "model_provider_id": str(provider.id),
            "model_name": FAKE_MODEL,
            "workflow_id": workflow["id"],
        },
    )
    assert bound.status_code == 201, bound.text
    agent_id = bound.json()["data"]["id"]
    conversation_id = await create_conversation(app_client, agent_id=agent_id)

    response = await app_client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "1+1"})
    assert response.status_code == 200, response.text
    events = await collect_sse(response.text)
    names = [name for name, _ in events]

    assert names[0] == "run.started"
    assert names[-1] == "done"
    assert [payload["node_id"] for name, payload in events if name == "workflow.node.started"] == [
        "start",
        "planner",
        "writer",
        "end",
    ]
    assert [payload["status"] for name, payload in events if name == "workflow.node.completed"] == ["succeeded"] * 4
    assert "run.completed" in names
    assert "message.delta" in names  # 节点内的 agent 输出照常流式

    # 节点产出的 assistant 消息进了同一个会话（会话是事实来源，2.6）
    messages = (await app_client.get(f"/api/v1/conversations/{conversation_id}/messages")).json()["data"]
    roles = [item["role"] for item in messages]
    assert roles.count("user") == 1 and roles.count("assistant") == 2
    contents = [item["content"] for item in messages if item["role"] == "assistant"]
    assert sorted(contents) == sorted(["FAKE_RESPONSE: 1+1", "FAKE_RESPONSE: 润色：FAKE_RESPONSE: 1+1"])

    # `runs` 行是 kind=workflow 且指向本次 WorkflowRun（2.6 / 4.8.1）
    runs = (await app_client.get("/api/v1/runs", params={"conversation_id": conversation_id})).json()["data"]
    assert len(runs) == 1
    assert runs[0]["kind"] == "workflow"
    assert runs[0]["workflow_run_id"]
    workflow_runs = (await app_client.get("/api/v1/workflow-runs", params={"workflow_id": workflow["id"]})).json()[
        "data"
    ]
    assert [item["id"] for item in workflow_runs] == [runs[0]["workflow_run_id"]]
    assert workflow_runs[0]["trigger"] == "api"
    assert workflow_runs[0]["status"] == "succeeded"


async def test_timeout_converges_run_and_node_rows(
    app_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 / W3 回归：`timeout_seconds` 到点后 `workflow_runs` / `runs` / `node_runs` 必须全部收敛。

    修复前的症状：`asyncio.wait_for` 的取消打断引擎里正在进行的 `commit()` → session 失效 →
    `_finalize` 抛 `PendingRollbackError` 穿透收尾 → 两行永久 `running`、没有 `workflow.run_finished`
    日志（实测超时 12s 后仍为 `running`，并已排除 `database is locked`）。
    """
    provider = await create_fake_provider(db_session, name="timeout-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="timeout-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="timeout-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    definition = with_agent_ids(
        definition_with(config={"max_steps": 10, "recursion_limit": 5, "timeout_seconds": 1}), agents
    )
    workflow = await create_workflow(app_client, agents, name="timeout", definition=definition)
    assert (await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")).status_code == 200

    monkeypatch.setattr(nodes_module, "execute_node", slow_node_execution(5.0))

    started = await start_workflow_run(app_client, workflow["id"], {"input": "1+1", "expr": "1"})
    run = await wait_for_terminal(app_client, started["id"], timeout=20.0)

    assert run["status"] == "failed"
    assert run["error_code"] == "RUN_TIMEOUT"
    assert run["ended_at"] is not None

    rows = await node_runs_of(app_client, started["id"])
    assert rows, "超时必须发生在节点执行中，否则这个用例没有覆盖到目标窗口"
    assert {row["status"] for row in rows} == {"canceled"}

    api_run = await api_run_of(db_session, started["id"])
    assert api_run.status == "failed"
    assert api_run.error_code == "RUN_TIMEOUT"
    assert api_run.canceled_at is None


async def test_cancel_active_run_records_canceled_at(
    app_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 回归：取消**活跃** Run 后 `runs.canceled_at` 必须回填。

    与 `run_service.cancel_run(...)` 直接落终态的分支保持同一语义（2.6）：查询"取消时间"时
    不该因为走的是 Workflow 路径就拿不到值。
    """
    provider = await create_fake_provider(db_session, name="active-cancel-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="active-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="active-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="active-cancel")
    assert (await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")).status_code == 200

    monkeypatch.setattr(nodes_module, "execute_node", slow_node_execution(1.0))

    started = await start_workflow_run(app_client, workflow["id"], {"input": "1+1", "expr": "1"})
    await wait_for_running_node(app_client, started["id"])

    cancel = await app_client.post(f"/api/v1/workflow-runs/{started['id']}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["data"]["status"] == "running"  # 活跃：只置信号，终态由后台任务收敛

    run = await wait_for_terminal(app_client, started["id"], timeout=20.0)
    assert run["status"] == "canceled"
    assert run["error_code"] == "RUN_CANCELED"

    api_run = await api_run_of(db_session, started["id"])
    assert api_run.status == "canceled"
    assert api_run.canceled_at is not None
    assert all(row["status"] != "running" for row in await node_runs_of(app_client, started["id"]))


async def test_shutdown_converges_active_runs(
    app_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2 回归：进程收尾取消后台任务时，Run 必须收敛为 `canceled`（不能停在 `running`）。

    lifespan 的 `finally` 会在 `dispose_engine()` 之前调 `shutdown_active_runs()` ——
    这里直接调同一个函数，等价于"进程正在优雅退出"的那一刻。
    """
    provider = await create_fake_provider(db_session, name="shutdown-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="shutdown-planner")
    writer = await create_agent(app_client, provider_id=provider.id, name="shutdown-writer")
    agents = {"planner": str(planner["id"]), "writer": str(writer["id"])}
    workflow = await create_workflow(app_client, agents, name="shutdown")
    assert (await app_client.post(f"/api/v1/workflows/{workflow['id']}/publish")).status_code == 200

    monkeypatch.setattr(nodes_module, "execute_node", slow_node_execution(30.0))

    started = await start_workflow_run(app_client, workflow["id"], {"input": "1+1", "expr": "1"})
    await wait_for_running_node(app_client, started["id"])

    canceled = await workflow_service.shutdown_active_runs(timeout=5.0)
    assert canceled >= 1

    await db_session.rollback()
    workflow_run = await db_session.get(WorkflowRun, started["id"])
    assert workflow_run is not None
    assert workflow_run.status == "canceled"
    assert workflow_run.error_code == "RUN_CANCELED"

    api_run = await api_run_of(db_session, started["id"])
    assert api_run.status == "canceled"
    assert api_run.canceled_at is not None
    assert all(row["status"] != "running" for row in await node_runs_of(app_client, started["id"]))
