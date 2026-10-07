"""服务层直测：工具（详细设计 3.2.3 / 4.2.2 / 2.5）。

与 `test_tools_api.py`（走 HTTP）互补：这里直接调 `tool_service`，把写端点的校验分支、
内置工具的"可禁用 / 可改权限"边界、删除时的审计保全与试跑路径都覆盖到
（9.3 的 `app/services/**` 覆盖率门禁；同 `test_agent_service.py` / `test_model_provider_service.py` 的定位）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import get_settings
from app.core.enums import RunKind, RunStatus
from app.core.errors import ConflictError, ToolNotFoundError, ValidationError
from app.db.models import Run, Tool, ToolInvocation
from app.runtime.tools.registry import builtin_tool_id
from app.schemas.tools import ToolCreate, ToolUpdate
from app.services import tool_service

API_PAYLOAD: dict[str, object] = {
    "name": "weather_now",
    "display_name": "实时天气",
    "description": "查询天气",
    "http_config": {"method": "get", "url": "https://api.example.com/weather"},
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    "permission_config": {"level": "guarded", "allow_network": True, "allowed_hosts": ["api.example.com"]},
    "tags": ["demo"],
}


def _create_payload(**overrides: object) -> ToolCreate:
    data = dict(API_PAYLOAD)
    data.update(overrides)
    return ToolCreate(**data)


@pytest.mark.asyncio
async def test_create_api_tool_persists_normalized_config(session: AsyncSession) -> None:
    tool = await tool_service.create_tool(session, _create_payload())

    assert tool.tool_type == "api" and tool.is_system is False and tool.builtin_name is None
    assert tool.http_config == {"method": "GET", "url": "https://api.example.com/weather"}
    assert tool.permission_config["level"] == "guarded" and tool.permission_config["max_calls_per_run"] == 20
    assert tool.tags == ["demo"]

    # 列表过滤：类型 / 状态 / 关键字
    assert [row.id for row in await tool_service.list_tools(session)] == [
        builtin_tool_id("calculator"),
        builtin_tool_id("file_read"),
        builtin_tool_id("file_write"),
        builtin_tool_id("python_execute"),
        builtin_tool_id("web_search"),
        tool.id,
    ]
    assert [row.id for row in await tool_service.list_tools(session, tool_type="api")] == [tool.id]
    assert await tool_service.list_tools(session, tool_type="builtin") != []
    assert await tool_service.list_tools(session, status="disabled") == []
    assert [row.id for row in await tool_service.list_tools(session, q="weather")] == [tool.id]
    assert [row.id for row in await tool_service.list_tools(session, q="天气")] == [tool.id]  # display_name 也匹配
    assert await tool_service.list_tools(session, q="nope") == []


@pytest.mark.asyncio
async def test_create_tool_rejects_conflicts_and_invalid_http_config(session: AsyncSession) -> None:
    await tool_service.create_tool(session, _create_payload())

    with pytest.raises(ConflictError) as duplicated:
        await tool_service.create_tool(session, _create_payload())
    assert duplicated.value.details == {"name": "weather_now"}

    with pytest.raises(ConflictError):
        await tool_service.create_tool(session, _create_payload(name="calculator"))  # 撞内置工具的名字

    with pytest.raises(ValidationError) as missing_url:
        await tool_service.create_tool(session, _create_payload(name="no_url", http_config={}))
    assert missing_url.value.details["field"] == "http_config.url"

    with pytest.raises(ValidationError) as bad_method:
        await tool_service.create_tool(
            session, _create_payload(name="bad_method", http_config={"url": "https://x.example", "method": "FETCH"})
        )
    assert bad_method.value.details["allowed"] == ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

    assert len(await tool_service.list_tools(session, tool_type="api")) == 1  # 失败不留半成品


@pytest.mark.asyncio
async def test_update_tool_respects_builtin_boundary_and_name_uniqueness(session: AsyncSession) -> None:
    file_write_id = builtin_tool_id("file_write")

    with pytest.raises(ValidationError) as denied:
        await tool_service.update_tool(session, file_write_id, ToolUpdate(description="运营改的描述"))
    assert denied.value.details["fields"] == ["description"]
    assert denied.value.details["editable"] == ["permission_config", "status", "tags"]

    updated = await tool_service.update_tool(
        session, file_write_id, ToolUpdate(status="disabled", permission_config={"level": "dangerous"})
    )
    assert updated.status == "disabled" and updated.permission_config["level"] == "dangerous"
    assert updated.description.startswith("把文本写入沙箱")  # 代码属地列未被改动

    api_tool = await tool_service.create_tool(session, _create_payload())
    renamed = await tool_service.update_tool(
        session,
        api_tool.id,
        ToolUpdate(name="weather_lookup", display_name="天气查询", tags=["renamed"]),
    )
    assert (renamed.name, renamed.display_name, renamed.tags) == ("weather_lookup", "天气查询", ["renamed"])

    with pytest.raises(ConflictError):
        await tool_service.update_tool(session, api_tool.id, ToolUpdate(name="calculator"))

    with pytest.raises(ValidationError):
        await tool_service.update_tool(session, api_tool.id, ToolUpdate(http_config={"method": "GET"}))

    with pytest.raises(ToolNotFoundError):
        await tool_service.update_tool(session, "01MISSING0000000000000000", ToolUpdate(status="disabled"))


@pytest.mark.asyncio
async def test_delete_tool_keeps_audit_rows_and_protects_builtins(session: AsyncSession) -> None:
    api_tool = await tool_service.create_tool(session, _create_payload())
    run = Run(id=ids.new_ulid(), kind=RunKind.CHAT, status=RunStatus.SUCCEEDED, input={}, output={})
    session.add(run)
    session.add(
        ToolInvocation(
            id=ids.new_ulid(),
            run_id=run.id,
            tool_id=api_tool.id,
            tool_name=api_tool.name,
            arguments={},
            normalized_arguments={},
            result="ok",
        )
    )
    await session.commit()

    await tool_service.delete_tool(session, api_tool.id)

    invocation = (await session.execute(select(ToolInvocation))).scalar_one()
    assert invocation.tool_id is None and invocation.tool_name == "weather_now"  # 2.5：删除不连带删审计
    with pytest.raises(ToolNotFoundError):
        await tool_service.get_tool(session, api_tool.id)

    with pytest.raises(ConflictError) as protected:
        await tool_service.delete_tool(session, builtin_tool_id("calculator"))
    assert protected.value.details["reason"] == "BUILTIN_TOOL_NOT_DELETABLE"
    assert await session.get(Tool, builtin_tool_id("calculator")) is not None


@pytest.mark.asyncio
async def test_run_tool_test_walks_the_pipeline_without_persisting(session: AsyncSession) -> None:
    settings = get_settings()

    row, ok = await tool_service.run_tool_test(
        session, builtin_tool_id("calculator"), settings=settings, arguments={"expression": "2+2"}
    )
    assert (row.name, ok.succeeded, ok.content) == ("calculator", True, "2+2 = 4")
    assert str(ok.permission_decision) == "allow" and ok.latency_ms >= 0

    _, invalid = await tool_service.run_tool_test(
        session, builtin_tool_id("calculator"), settings=settings, arguments={"expression": ""}
    )
    assert invalid.succeeded is False and invalid.error_code == "TOOL_INVALID_ARGUMENTS"

    _, denied = await tool_service.run_tool_test(
        session,
        builtin_tool_id("file_write"),
        settings=settings,
        arguments={"path": "outputs/a.txt", "content": "hi"},
    )
    assert (denied.error_code, str(denied.permission_decision)) == ("TOOL_PERMISSION_DENIED", "deny")
    assert denied.meta["reason"] == "SWITCH_DISABLED"  # SD-17：未开启即拒绝

    await tool_service.update_tool(session, builtin_tool_id("calculator"), ToolUpdate(status="disabled"))
    _, disabled = await tool_service.run_tool_test(session, builtin_tool_id("calculator"), settings=settings)
    assert disabled.error_code == "TOOL_DISABLED"

    with pytest.raises(ToolNotFoundError):
        await tool_service.run_tool_test(session, "01MISSING0000000000000000", settings=settings)

    # 试跑不落 `tool_invocations`（没有可挂的 `run_id`）
    assert (await session.execute(select(ToolInvocation))).scalars().all() == []
