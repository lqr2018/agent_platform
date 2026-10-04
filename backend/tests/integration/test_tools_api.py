"""工具 API 与启动对齐（详细设计 3.2.3 / 4.2.2 / 6.3 第 2 条）。

`tools` 的种子由迁移 `0003` 写入，启动时再由 `sync_builtin_tools` 与代码定义对齐；
`GET /tools` 是前端工具管理页的唯一读入口，这里锁定它的字段、过滤与"运营改动不被重启抹掉"。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tool
from app.runtime.tools.registry import BUILTIN_TOOL_IDS, builtin_tool_id
from app.services import tool_service

BUILTIN_NAMES = sorted(BUILTIN_TOOL_IDS)


@pytest.mark.asyncio
async def test_list_tools_returns_seeded_builtins(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/tools")

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert [item["name"] for item in data] == BUILTIN_NAMES
    assert body["meta"]["request_id"]

    calculator = next(item for item in data if item["name"] == "calculator")
    assert calculator["id"] == builtin_tool_id("calculator")
    assert calculator["display_name"] == "计算器"
    assert (calculator["tool_type"], calculator["status"]) == ("builtin", "enabled")
    assert calculator["is_system"] is True
    assert calculator["input_schema"]["required"] == ["expression"]
    assert calculator["permission_config"]["level"] == "safe"
    # 列表不暴露 `http_config`（可能含敏感请求头）与 `mcp_*` 列（SD-16：MVP 恒为 NULL）
    assert "http_config" not in calculator
    assert "mcp_server_id" not in calculator and "mcp_tool_name" not in calculator


@pytest.mark.asyncio
async def test_list_tools_filters(app_client: AsyncClient) -> None:
    matched = await app_client.get("/api/v1/tools", params={"tool_type": "builtin", "status": "enabled", "q": "calc"})
    assert [item["name"] for item in matched.json()["data"]] == ["calculator"]

    assert (await app_client.get("/api/v1/tools", params={"status": "disabled"})).json()["data"] == []
    assert (await app_client.get("/api/v1/tools", params={"tool_type": "api"})).json()["data"] == []


@pytest.mark.asyncio
async def test_tool_detail_includes_http_config_and_404_envelope(app_client: AsyncClient) -> None:
    found = await app_client.get(f"/api/v1/tools/{builtin_tool_id('file_read')}")

    assert found.status_code == 200
    assert found.json()["data"]["http_config"] == {}

    missing = await app_client.get("/api/v1/tools/01MISSING0000000000000000")
    assert missing.status_code == 404
    body = missing.json()
    assert body["error"]["code"] == "TOOL_NOT_FOUND"
    assert body["meta"]["request_id"] == missing.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_tool_invocations_endpoint_is_empty_without_runs(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/tool-invocations")
    assert response.status_code == 200 and response.json()["data"] == []


@pytest.mark.asyncio
async def test_startup_sync_keeps_code_definition_and_operator_state(
    app_client: AsyncClient, session: AsyncSession
) -> None:
    """4.2.2：`description` 以代码为准；`status` / `permission_config` 是运营侧的地盘，重启不许抹掉。"""
    tool = await session.get(Tool, builtin_tool_id("calculator"))
    assert tool is not None
    tool.description = "被运营改坏的描述"
    tool.status = "disabled"
    tool.permission_config = {"level": "dangerous", "max_calls_per_run": 1}
    await session.commit()

    counts = await tool_service.sync_builtin_tools(session)
    await session.refresh(tool)

    assert counts == {"inserted": 0, "updated": 1, "unchanged": 4}
    assert tool.description.startswith("计算数学表达式")
    assert tool.status == "disabled"
    assert tool.permission_config == {"level": "dangerous", "max_calls_per_run": 1}

    # 幂等：再跑一次不再产生写入
    assert await tool_service.sync_builtin_tools(session) == {"inserted": 0, "updated": 0, "unchanged": 5}
