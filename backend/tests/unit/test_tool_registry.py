"""工具注册表与迁移种子的一致性（详细设计 4.2.1 / 4.2.2 / 7.3）。

迁移 `0003` 的 `TOOL_SEED_ROWS` **不能** import app 代码（历史迁移必须可重放），
因此它与 `registry.seed_rows()` 是两份字面副本 —— 本模块用逐字段比对把这对副本钉在一起。
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType

from app.runtime.tools.base import TOOL_STATUS_ENABLED, ToolPermissionConfig
from app.runtime.tools.builtin import BUILTIN_TOOLS
from app.runtime.tools.registry import (
    BUILTIN_TOOL_IDS,
    ToolRegistry,
    builtin_definitions,
    builtin_tool_id,
    default_registry,
    seed_rows,
)

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0003_phase2_tool_tables.py"

SEED_ROW_KEYS = {
    "id",
    "name",
    "display_name",
    "description",
    "tool_type",
    "status",
    "input_schema",
    "output_schema",
    "permission_config",
    "builtin_name",
    "http_config",
    "is_system",
}
"""`tools` 表在种子数据里声明的列（2.5；其余列由迁移补默认值）。"""

BUILTIN_ORDER = ["calculator", "file_read", "file_write", "web_search", "python_execute"]
"""2.5 表格顺序 = `builtin/__init__.py::BUILTIN_TOOLS` 的顺序（稳定 id 按此登记）。"""


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0003_phase2_tool_tables", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builtin_ids_are_stable_and_unique() -> None:
    """稳定 id：值写死、全局唯一（随机会让已持久化的 `agents.tool_ids` 失效）。

    注：这 5 个字面量是 **25** 字符，比 0.2.1 的"26 位 ULID"短一位（`tools.id` 列声明为
    `String(26)`，SQLite/PG 均接受）。它们已被迁移 `0003` 与 `agents.tool_ids` 引用，
    **不能**随手改（改名会撞 `tools.name` 的 UNIQUE）；是否补齐到 26 位留作独立决策。
    """
    assert set(BUILTIN_TOOL_IDS) == set(BUILTIN_ORDER)
    ids = list(BUILTIN_TOOL_IDS.values())
    assert len(set(ids)) == len(ids)
    assert {len(item) for item in ids} == {25}
    assert ids == [f"01J0T0000000000000000000{index}" for index in range(1, 6)]
    assert builtin_tool_id("calculator") == "01J0T00000000000000000001"
    assert builtin_tool_id("python_execute") == "01J0T00000000000000000005"


def test_builtin_tools_package_order_matches_document() -> None:
    """`BUILTIN_TOOLS` 的顺序即 2.5 表格顺序（`registry` 的 id 表按同一顺序登记）。"""
    assert [tool.name for tool in BUILTIN_TOOLS] == BUILTIN_ORDER


def test_seed_rows_match_migration_literals_field_by_field() -> None:
    """迁移种子 == 代码定义（4.2.2：内置工具定义的单一来源是代码）。"""
    code_rows = {row["name"]: row for row in seed_rows()}
    migration_rows = {row["name"]: row for row in _load_migration().TOOL_SEED_ROWS}

    assert set(code_rows) == set(migration_rows) == set(BUILTIN_ORDER)
    for name, code_row in code_rows.items():
        assert set(code_row) == SEED_ROW_KEYS
        assert set(migration_rows[name]) == SEED_ROW_KEYS
        assert migration_rows[name] == code_row, f"seed row mismatch for {name}"


def test_seed_rows_are_json_serializable() -> None:
    """种子经 `bulk_insert` 落库：JSON 列必须先能序列化（不能塞 Pydantic 模型）。"""
    for row in seed_rows():
        json.dumps(row["input_schema"])
        json.dumps(row["output_schema"])
        json.dumps(row["permission_config"])
        assert row["http_config"] == {}
        assert row["is_system"] is True
        assert row["status"] == TOOL_STATUS_ENABLED
        assert row["tool_type"] == "builtin"


def test_code_definitions_carry_schema_and_permission() -> None:
    """代码侧定义（`builtin_definitions()`）是 `seed_rows()` 的上游，字段必须齐备。"""
    definitions = builtin_definitions()
    assert [item.name for item in definitions] == sorted(BUILTIN_ORDER)
    for definition in definitions:
        assert definition.id == builtin_tool_id(definition.name)
        assert definition.builtin_name == definition.name
        assert definition.description.strip()
        assert definition.input_schema.get("type") == "object"
        assert isinstance(definition.permission_config, ToolPermissionConfig)


def test_visible_definitions_filters_disabled_and_keeps_agent_order() -> None:
    """4.2.2：`tool_ids` 顺序即下发顺序；`status != enabled` 与未登记 id 一律不出现。"""
    definitions = builtin_definitions()
    patched = [replace(item, status="disabled") if item.name == "web_search" else item for item in definitions]

    registry = default_registry()
    ordered = [
        builtin_tool_id("file_write"),
        builtin_tool_id("calculator"),
        builtin_tool_id("web_search"),  # 已被禁用 → 不进结果
        "01J0UNKNOWN0000000000000000",  # 未登记 id → 不进结果
        builtin_tool_id("file_read"),
    ]
    visible = registry.visible_definitions(patched, agent_tool_ids=ordered)

    assert [item.name for item in visible] == ["file_write", "calculator", "file_read"]
    assert registry.visible_definitions(definitions, agent_tool_ids=[]) == []


def test_function_schemas_shape_matches_design_example() -> None:
    """4.2.2 的 function schema：`parameters` 就是 `input_schema`，description 直接进 schema。"""
    registry = default_registry()
    definitions = registry.visible_definitions(builtin_definitions(), agent_tool_ids=[builtin_tool_id("calculator")])
    schemas = ToolRegistry.function_schemas(definitions)

    assert len(schemas) == 1
    function = schemas[0]["function"]
    assert schemas[0]["type"] == "function"
    assert function["name"] == "calculator"
    assert function["description"] == definitions[0].description
    assert function["parameters"] == dict(definitions[0].input_schema)
    assert function["parameters"]["required"] == ["expression"]


def test_registry_require_and_lookup() -> None:
    registry = default_registry()
    assert registry.names() == sorted(BUILTIN_ORDER)
    assert len(registry) == len(BUILTIN_ORDER)
    assert "calculator" in registry
    assert registry.get("nope") is None
    assert registry.require("calculator").name == "calculator"
