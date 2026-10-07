"""工具 DTO 与 runtime 权限模型的「两份副本」一致性（详细设计 3.2.3 / 2.5 / 1.2）。

`api` 层不 import `runtime`（1.2：只做协议转换），所以 `permission_config` 的请求侧模型
（`schemas/tools.py::ToolPermissionConfigDTO`）与 runtime 的 `ToolPermissionConfig` 是**两份字面副本**
—— 本模块逐字段比对把这对副本钉在一起（同 `test_tool_registry.py` 对迁移种子的做法）。

同时锁定 `ToolCreate` 的收口：`name` 必须是 LLM 可用的函数名，`tool_type` 只能是 `api`。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.runtime.tools.base import ToolPermissionConfig
from app.schemas.tools import HTTP_METHODS, TOOL_NAME_PATTERN, ToolCreate, ToolPermissionConfigDTO, ToolUpdate


def test_permission_config_dto_mirrors_runtime_model_field_by_field() -> None:
    dto_fields = ToolPermissionConfigDTO.model_fields
    runtime_fields = ToolPermissionConfig.model_fields

    assert set(dto_fields) == set(runtime_fields)
    for name, runtime_field in runtime_fields.items():
        assert dto_fields[name].annotation == runtime_field.annotation, f"annotation mismatch for {name}"

    # 默认值也一致：空请求体（`{}`）在两侧折叠出同一份配置
    assert ToolPermissionConfigDTO().model_dump() == ToolPermissionConfig().model_dump()


def test_tool_create_defaults_are_minimal_and_api_only() -> None:
    tool = ToolCreate(name="weather_now")

    assert tool.tool_type == "api"  # 只建 api 类型（builtin 由迁移维护，mcp 属 SD-16）
    assert tool.status == "enabled"
    assert tool.display_name == "" and tool.description == ""
    assert tool.input_schema == {"type": "object", "properties": {}}
    assert tool.output_schema == {} and tool.http_config == {} and tool.tags == []
    assert tool.permission_config.level == "safe"
    assert TOOL_NAME_PATTERN == r"^[a-z][a-z0-9_]{0,63}$"


@pytest.mark.parametrize("name", ["Weather", "weather-now", "1weather", "weather now", "天气", "a" * 65])
def test_tool_create_rejects_invalid_function_names(name: str) -> None:
    with pytest.raises(PydanticValidationError):
        ToolCreate(name=name)


@pytest.mark.parametrize("tool_type", ["builtin", "mcp", "MCP", ""])
def test_tool_create_rejects_non_api_types(tool_type: str) -> None:
    with pytest.raises(PydanticValidationError):
        ToolCreate(name="weather_now", tool_type=tool_type)  # type: ignore[arg-type]


def test_tool_create_strips_name_and_accepts_leading_underscore_free_names() -> None:
    assert ToolCreate(name="  weather_now  ").name == "weather_now"
    assert ToolCreate(name="calculator_v2").name == "calculator_v2"


def test_permission_config_dto_validates_ranges_and_ignores_unknown_keys() -> None:
    config = ToolPermissionConfigDTO.model_validate({"level": "guarded", "max_calls_per_run": 3, "unknown": 1})
    assert (config.level, config.max_calls_per_run, config.timeout_seconds) == ("guarded", 3, 15.0)

    for payload in ({"max_output_bytes": 0}, {"max_calls_per_run": 0}, {"timeout_seconds": 0}, {"level": "nope"}):
        with pytest.raises(PydanticValidationError):
            ToolPermissionConfigDTO.model_validate(payload)


def test_tool_update_is_a_partial_payload() -> None:
    assert ToolUpdate().model_dump(exclude_unset=True) == {}
    assert ToolUpdate(status="disabled").model_dump(exclude_unset=True) == {"status": "disabled"}
    assert ToolUpdate(name="  calculator_v2 ").name == "calculator_v2"
    assert HTTP_METHODS == ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
