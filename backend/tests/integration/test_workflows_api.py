"""Workflow API 集成测试（详细设计 3.2.7 / 7.4）。

覆盖：CRUD、图校验（`/validate` 不合法也回 200）、发布、删除、错误码
（404 `WORKFLOW_NOT_FOUND` / 409 重名 / 422 `WORKFLOW_INVALID_GRAPH`）。
端到端运行、resume、取消、Chat 内联见 `test_workflow_runner.py`。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_agent, create_fake_provider

PLANNER = "planner"
WRITER = "writer"


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（与 `test_agents_api.py` 同一写法）。"""
    return session


def definition_with(**overrides: Any) -> dict[str, Any]:
    """标准图（与单测同构）：`start → planner(agent) → calc(tool) → router(condition) → writer → end`。"""
    definition: dict[str, Any] = {
        "name": "canonical",
        "nodes": [
            {"id": "start", "type": "start", "next": "planner"},
            {
                "id": PLANNER,
                "type": "agent",
                "agent_id": "AGENT_PLANNER",
                "input_template": "{{state.input}}",
                "output_key": "plan",
                "next": "calc",
            },
            {
                "id": "calc",
                "type": "tool",
                "tool_name": "calculator",
                "arguments_template": {"expression": "{{state.expr}}"},
                "output_key": "result",
                "next": "router",
            },
            {
                "id": "router",
                "type": "condition",
                "branches": [{"when": "state.expr != ''", "next": WRITER}],
                "default_next": "end",
            },
            {
                "id": WRITER,
                "type": "agent",
                "agent_id": "AGENT_WRITER",
                "input_template": "补充说明：{{state.result}}",
                "output_key": "draft",
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
        "config": {"max_steps": 10, "recursion_limit": 5, "timeout_seconds": 60},
    }
    definition.update(overrides)
    return definition


@pytest.fixture
async def graph_agents(app_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    """图里引用的两个 Agent（走 API 建，引用校验才能通过）。"""
    provider = await create_fake_provider(db_session, name="wf-provider")
    planner = await create_agent(app_client, provider_id=provider.id, name="wf-planner", system_prompt="你是规划员。")
    writer = await create_agent(app_client, provider_id=provider.id, name="wf-writer", system_prompt="你是撰稿人。")
    return {"planner": str(planner["id"]), "writer": str(writer["id"])}


def with_agent_ids(definition: dict[str, Any], agents: dict[str, str]) -> dict[str, Any]:
    payload = copy.deepcopy(definition)
    for node in payload["nodes"]:
        if node.get("agent_id") == "AGENT_PLANNER":
            node["agent_id"] = agents["planner"]
        if node.get("agent_id") == "AGENT_WRITER":
            node["agent_id"] = agents["writer"]
    return payload


async def create_workflow(
    client: AsyncClient,
    agents: dict[str, str],
    *,
    name: str = "canonical",
    definition: dict[str, Any] | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "description": "测试用图",
            "status": status,
            "state_schema": {"input": {"type": "string"}, "expr": {"type": "string"}},
            "definition": definition or with_agent_ids(definition_with(), agents),
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


async def test_tool_name_must_exist(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    """4.5.1：`tool` 节点引用的工具必须存在且 enabled。"""
    definition = with_agent_ids(definition_with(), graph_agents)
    definition["nodes"][2]["tool_name"] = "ghost_tool"
    response = await app_client.post("/api/v1/workflows", json={"name": "bad-tool", "definition": definition})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WORKFLOW_INVALID_GRAPH"
    assert "TOOL_NOT_FOUND" in {item["code"] for item in response.json()["error"]["details"]["errors"]}


async def test_workflow_crud_round_trip(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    created = await create_workflow(app_client, graph_agents)
    assert created["status"] == "draft"
    assert created["version"] == 1
    assert created["node_count"] == 6
    assert created["start_node_id"] == "start"

    workflow_id = created["id"]
    listing = await app_client.get("/api/v1/workflows", params={"status": "draft"})
    assert [row["id"] for row in listing.json()["data"]] == [workflow_id]
    assert (await app_client.get("/api/v1/workflows", params={"q": "不存在"})).json()["data"] == []

    detail = await app_client.get(f"/api/v1/workflows/{workflow_id}")
    assert detail.json()["data"]["definition"]["config"]["max_steps"] == 10

    renamed = await app_client.patch(f"/api/v1/workflows/{workflow_id}", json={"name": "canonical-2"})
    assert renamed.json()["data"]["name"] == "canonical-2"

    deleted = await app_client.delete(f"/api/v1/workflows/{workflow_id}")
    assert deleted.status_code == 204
    missing = await app_client.get(f"/api/v1/workflows/{workflow_id}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_duplicate_name_conflicts(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    await create_workflow(app_client, graph_agents)
    again = await app_client.post(
        "/api/v1/workflows",
        json={"name": "canonical", "definition": with_agent_ids(definition_with(), graph_agents)},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


async def test_invalid_graph_is_rejected_on_create(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    """SD-1：多出边 → `PARALLEL_EDGES_NOT_SUPPORTED`；缺失 agent 引用 → `AGENT_NOT_FOUND`。"""
    definition = with_agent_ids(definition_with(), graph_agents)
    definition["edges"] = [{"from": "calc", "to": "router"}, {"from": "calc", "to": "end"}]
    response = await app_client.post("/api/v1/workflows", json={"name": "bad-graph", "definition": definition})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WORKFLOW_INVALID_GRAPH"
    codes = {item["code"] for item in response.json()["error"]["details"]["errors"]}
    assert "PARALLEL_EDGES_NOT_SUPPORTED" in codes

    unknown_agent = with_agent_ids(definition_with(), graph_agents)
    unknown_agent["nodes"][1]["agent_id"] = "01J8Z0000000000000000000A1"
    missing = await app_client.post("/api/v1/workflows", json={"name": "bad-agent", "definition": unknown_agent})
    assert missing.status_code == 422
    assert "AGENT_NOT_FOUND" in {item["code"] for item in missing.json()["error"]["details"]["errors"]}


async def test_validate_endpoint_reports_issues_and_projection(
    app_client: AsyncClient, graph_agents: dict[str, str]
) -> None:
    """3.2.7：`/validate` **只校验不落库** —— 不合法也回 200，把错误清单与只读图交给编辑器。"""
    created = await create_workflow(app_client, graph_agents)
    workflow_id = created["id"]

    ok = await app_client.post(f"/api/v1/workflows/{workflow_id}/validate")
    assert ok.status_code == 200
    body = ok.json()["data"]
    assert body["valid"] is True and body["errors"] == []
    assert body["graph"]["start_node_id"] == "start"
    assert [node["id"] for node in body["graph"]["nodes"]] == [
        "start",
        PLANNER,
        "calc",
        "router",
        WRITER,
        "end",
    ]

    draft = with_agent_ids(definition_with(), graph_agents)
    draft["nodes"] = [node for node in draft["nodes"] if node["id"] != "end"]
    invalid = await app_client.post(f"/api/v1/workflows/{workflow_id}/validate", json={"definition": draft})
    assert invalid.status_code == 200
    body = invalid.json()["data"]
    assert body["valid"] is False and body["graph"] is None
    assert "END_NODE_MISSING" in {item["code"] for item in body["errors"]}

    # 草稿校验不会落库（1.2：写路径与校验路径分离）
    assert (await app_client.get(f"/api/v1/workflows/{workflow_id}")).json()["data"]["node_count"] == 6


async def test_definition_change_resets_status_to_draft(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    """2.9：发布后改定义 → 回 `draft`（已发布版本的语义不能悄悄漂移）。"""
    created = await create_workflow(app_client, graph_agents)
    workflow_id = created["id"]

    published = await app_client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert published.status_code == 200
    assert published.json()["data"]["status"] == "published"
    assert published.json()["data"]["version"] == 2

    changed = await app_client.patch(
        f"/api/v1/workflows/{workflow_id}",
        json={"definition": with_agent_ids(definition_with(), graph_agents)},
    )
    body = changed.json()["data"]
    assert body["status"] == "draft"
    assert body["version"] == 2  # 版本号只在 publish 时 +1

    archived = await app_client.patch(f"/api/v1/workflows/{workflow_id}", json={"status": "archived"})
    assert archived.json()["data"]["status"] == "archived"


async def test_delete_is_blocked_while_a_run_is_active(
    app_client: AsyncClient, graph_agents: dict[str, str], db_session: AsyncSession
) -> None:
    """删除前先确认没有运行中的实例（避免删掉正在跑的图）。"""
    from app.db.models import WorkflowRun

    created = await create_workflow(app_client, graph_agents)
    db_session.add(
        WorkflowRun(
            workflow_id=created["id"],
            workflow_version=1,
            definition_snapshot=created["definition"],
            status="running",
            trigger="manual",
            input={},
            output={},
            state={},
            checkpoint={},
        )
    )
    await db_session.commit()

    response = await app_client.delete(f"/api/v1/workflows/{created['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "WORKFLOW_HAS_RUNNING_RUNS"


async def test_archived_workflow_cannot_be_started(app_client: AsyncClient, graph_agents: dict[str, str]) -> None:
    """`archived` 表示"退役"：不允许再启动运行（试跑只对 draft / published 开放）。"""
    created = await create_workflow(app_client, graph_agents)
    await app_client.patch(f"/api/v1/workflows/{created['id']}", json={"status": "archived"})

    response = await app_client.post(f"/api/v1/workflows/{created['id']}/runs", json={"input": {}})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "WORKFLOW_ARCHIVED"


async def test_missing_workflow_returns_404(app_client: AsyncClient) -> None:
    assert (await app_client.get("/api/v1/workflows/01J8Z0000000000000000000Z1")).status_code == 404
    assert (await app_client.post("/api/v1/workflows/01J8Z0000000000000000000Z1/publish")).status_code == 404
    assert (await app_client.get("/api/v1/workflow-runs/01J8Z0000000000000000000Z1")).status_code == 404
