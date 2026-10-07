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


# --------------------------------------------------------------------------------------
# 写端点（3.2.3）：POST / PATCH / DELETE / POST /{id}/test
# --------------------------------------------------------------------------------------
API_TOOL_PAYLOAD: dict[str, object] = {
    "name": "weather_now",
    "display_name": "实时天气",
    "description": "查询指定城市的实时天气",
    "tool_type": "api",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
    "http_config": {"method": "get", "url": "https://api.example.com/weather", "query": {"q": "{{city}}"}},
    "permission_config": {"level": "guarded", "allow_network": True, "allowed_hosts": ["api.example.com"]},
    "tags": ["demo"],
}


@pytest.mark.asyncio
async def test_create_api_tool_normalizes_config_and_lists_it(app_client: AsyncClient) -> None:
    created = await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD)

    assert created.status_code == 201
    tool = created.json()["data"]
    assert tool["tool_type"] == "api" and tool["is_system"] is False and tool["builtin_name"] is None
    assert tool["http_config"]["method"] == "GET"  # 归一为大写
    assert tool["permission_config"] == {
        "level": "guarded",
        "require_approval": False,
        "allowed_paths": [],
        "allow_network": True,
        "allowed_hosts": ["api.example.com"],
        "max_output_bytes": 65_536,
        "timeout_seconds": 15.0,
        "max_calls_per_run": 20,
    }
    assert tool["tags"] == ["demo"]

    listed = (await app_client.get("/api/v1/tools", params={"tool_type": "api"})).json()["data"]
    assert [item["name"] for item in listed] == ["weather_now"]
    assert "http_config" not in listed[0]  # 列表不暴露请求头（3.2.3）


@pytest.mark.asyncio
async def test_create_tool_rejects_duplicate_name_and_bad_config(app_client: AsyncClient) -> None:
    assert (await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD)).status_code == 201

    duplicated = await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD)
    assert duplicated.status_code == 409
    assert duplicated.json()["error"]["code"] == "CONFLICT"
    assert (await app_client.post("/api/v1/tools", json=dict(API_TOOL_PAYLOAD, name="calculator"))).status_code == 409

    missing_url = dict(API_TOOL_PAYLOAD, name="no_url", http_config={"method": "GET"})
    refused = await app_client.post("/api/v1/tools", json=missing_url)
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["field"] == "http_config.url"

    bad_method = dict(
        API_TOOL_PAYLOAD, name="bad_method", http_config={"url": "https://api.example.com", "method": "FETCH"}
    )
    assert (await app_client.post("/api/v1/tools", json=bad_method)).status_code == 422

    # `builtin` / `mcp` 不在 `ToolCreate.tool_type` 的取值里（SD-16：MVP 不可建 mcp）
    for tool_type in ("builtin", "mcp"):
        payload = dict(API_TOOL_PAYLOAD, name=f"x_{tool_type}", tool_type=tool_type)
        assert (await app_client.post("/api/v1/tools", json=payload)).status_code == 422

    assert (await app_client.post("/api/v1/tools", json=dict(API_TOOL_PAYLOAD, name="Weather-Now"))).status_code == 422


@pytest.mark.asyncio
async def test_create_tool_is_idempotent_with_header(app_client: AsyncClient) -> None:
    """1.5.5：同一个 `Idempotency-Key` 重放返回首次结果，不产生第二行。"""
    headers = {"Idempotency-Key": "tool-create-1"}
    first = await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD, headers=headers)
    second = await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    listed = (await app_client.get("/api/v1/tools", params={"tool_type": "api"})).json()["data"]
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_patch_builtin_tool_only_allows_operator_owned_fields(app_client: AsyncClient) -> None:
    """2.5：内置工具"可禁用 / 可改权限"；代码属地的列（description 等）→ 422。"""
    tool_id = builtin_tool_id("file_write")

    denied = await app_client.patch(f"/api/v1/tools/{tool_id}", json={"description": "运营改的描述"})
    assert denied.status_code == 422
    body = denied.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {
        "tool_id": tool_id,
        "fields": ["description"],
        "editable": ["permission_config", "status", "tags"],
        "reason": "BUILTIN_DEFINITION_IS_CODE_OWNED",
    }

    allowed = await app_client.patch(
        f"/api/v1/tools/{tool_id}",
        json={"status": "disabled", "permission_config": {"level": "dangerous"}, "tags": ["ops"]},
    )
    assert allowed.status_code == 200
    data = allowed.json()["data"]
    assert (data["status"], data["permission_config"]["level"], data["tags"]) == ("disabled", "dangerous", ["ops"])
    assert data["description"].startswith("把文本写入沙箱")  # 代码属地列未被改动


@pytest.mark.asyncio
async def test_patch_and_delete_api_tool_round_trip(app_client: AsyncClient) -> None:
    tool_id = (await app_client.post("/api/v1/tools", json=API_TOOL_PAYLOAD)).json()["data"]["id"]

    renamed = await app_client.patch(
        f"/api/v1/tools/{tool_id}",
        json={"name": "weather_lookup", "display_name": "天气查询", "status": "disabled"},
    )
    assert renamed.status_code == 200
    assert (renamed.json()["data"]["name"], renamed.json()["data"]["status"]) == ("weather_lookup", "disabled")

    deleted = await app_client.delete(f"/api/v1/tools/{tool_id}")
    assert deleted.status_code == 204
    assert (await app_client.get(f"/api/v1/tools/{tool_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_builtin_tool_is_rejected(app_client: AsyncClient) -> None:
    response = await app_client.delete(f"/api/v1/tools/{builtin_tool_id('calculator')}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["details"]["reason"] == "BUILTIN_TOOL_NOT_DELETABLE"
    assert (await app_client.get(f"/api/v1/tools/{builtin_tool_id('calculator')}")).status_code == 200


@pytest.mark.asyncio
async def test_tool_test_endpoint_runs_the_pipeline(app_client: AsyncClient) -> None:
    """3.2.3：`POST /tools/{id}/test` 走同一套九步流水线（跳过 LLM）。"""
    ok = await app_client.post(
        f"/api/v1/tools/{builtin_tool_id('calculator')}/test",
        json={"arguments": {"expression": "2+2"}},
    )
    assert ok.status_code == 200
    payload = ok.json()["data"]
    assert payload["ok"] is True
    assert (payload["tool_name"], payload["status"]) == ("calculator", "succeeded")
    assert payload["permission_decision"] == "allow"
    assert payload["result"] == "2+2 = 4" and payload["truncated"] is False and payload["latency_ms"] >= 0

    invalid = await app_client.post(
        f"/api/v1/tools/{builtin_tool_id('calculator')}/test",
        json={"arguments": {"expression": ""}},
    )
    invalid_payload = invalid.json()["data"]
    assert invalid.status_code == 200 and invalid_payload["ok"] is False
    assert invalid_payload["error_code"] == "TOOL_INVALID_ARGUMENTS"
    assert invalid_payload["details"]["tool_name"] == "calculator"

    # 请求体可省略（等价于 `{}`）→ 参数校验失败，而不是 422 / 500
    no_body = await app_client.post(f"/api/v1/tools/{builtin_tool_id('calculator')}/test")
    assert no_body.status_code == 200 and no_body.json()["data"]["error_code"] == "TOOL_INVALID_ARGUMENTS"

    missing = await app_client.post("/api/v1/tools/01MISSING0000000000000000/test", json={"arguments": {}})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_tool_test_respects_permission_config_written_via_patch(app_client: AsyncClient) -> None:
    """DoD 2：改 `permission_config` 后**立即生效**（配置 → 执行期权限判定的闭环）。

    `file_write` 的种子配置是 `guarded + require_approval=true` → 未开启 `FILE_WRITE_ENABLED`
    时被"未开启即拒绝"（SD-17，`reason=SWITCH_DISABLED`）；把 `level` 改成 `dangerous`
    （请求里的 DTO 默认 `require_approval=false`）后，拒绝理由变为 `DANGEROUS_TOOL_DISABLED`
    —— 同一次调用、不同结论，证明确实读的是 DB 里的权限配置。
    """
    tool_id = builtin_tool_id("file_write")
    arguments = {"arguments": {"path": "outputs/a.txt", "content": "hello"}}

    guarded = (await app_client.post(f"/api/v1/tools/{tool_id}/test", json=arguments)).json()["data"]
    assert guarded["ok"] is False
    assert guarded["error_code"] == "TOOL_PERMISSION_DENIED"
    assert (guarded["permission_decision"], guarded["details"]["reason"]) == ("deny", "SWITCH_DISABLED")

    patched = await app_client.patch(f"/api/v1/tools/{tool_id}", json={"permission_config": {"level": "dangerous"}})
    assert patched.status_code == 200 and patched.json()["data"]["permission_config"]["level"] == "dangerous"

    dangerous = (await app_client.post(f"/api/v1/tools/{tool_id}/test", json=arguments)).json()["data"]
    assert dangerous["ok"] is False
    assert (dangerous["permission_decision"], dangerous["details"]["reason"]) == ("deny", "DANGEROUS_TOOL_DISABLED")

    # 试跑不落 `tool_invocations`（没有 `run_id` 外键可挂）：3.2.3 的"跳过 LLM"只在内存里执行
    assert (await app_client.get("/api/v1/tool-invocations")).json()["data"] == []
