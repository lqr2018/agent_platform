"""工具注册表（详细设计 4.2.1 / 4.2.2）。

两个职责：

1. **内置工具登记**：`BUILTIN_TOOL_IDS` + `BUILTIN_TOOLS` 是 5 个内置工具的**唯一代码来源**；
   迁移 `0003_phase2_tool_tables` 的种子数据与之逐字段一致（`test_tool_registry.py` 断言 ——
   迁移不 import app 代码，否则代码演进会让历史迁移不可重放）；
2. **可见性 + schema 生成**（4.2.2）：只把 Agent 引用且 `status=enabled` 的工具交给 LLM，
   `tool_ids` 的顺序即 prompt 顺序（2.4）。

`ToolRegistry` 不做全局单例：服务层按需构造，测试注入替身（9.2）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.runtime.tools.base import BaseTool, ToolDefinition

BUILTIN_TOOL_IDS: Mapping[str, str] = {
    "calculator": "01J0T00000000000000000001",
    "file_read": "01J0T00000000000000000002",
    "file_write": "01J0T00000000000000000003",
    "web_search": "01J0T00000000000000000004",
    "python_execute": "01J0T00000000000000000005",
}
"""内置工具的**稳定 id**（26 字符、Crocksford 字符集，避开 I/L/O/U）。

固定 id 的理由：种子数据必须幂等（重复 `alembic upgrade` 与 `test_*` 反复建库都指向同一行），
且 `agents.tool_ids` 会持久化它们 —— 随机 ULID 会让不同实例指向不同行。
"""


def builtin_tool_id(name: str) -> str:
    """按工具名取稳定 id；未登记的名字直接失败（防拼写错误静默通过）。"""
    return BUILTIN_TOOL_IDS[name]


class ToolRegistry:
    """内置工具的内存注册表。"""

    def __init__(self, tools: Iterable[BaseTool] | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError("tool.name must not be empty")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def require(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ---- 可见性与 schema（4.2.2） ----
    def visible_definitions(
        self,
        definitions: Sequence[ToolDefinition],
        *,
        agent_tool_ids: Sequence[str] = (),
    ) -> list[ToolDefinition]:
        """过滤 `status=enabled` 并保持 `agent_tool_ids` 顺序；空 → 本轮不带工具。"""
        by_id = {item.id: item for item in definitions}
        ordered: list[ToolDefinition] = []
        for tool_id in agent_tool_ids:
            candidate = by_id.get(tool_id)
            if candidate is not None and candidate.is_enabled:
                ordered.append(candidate)
        return ordered

    @staticmethod
    def function_schemas(definitions: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        """组装 `tools=[{"type": "function", ...}]`（4.2.2）；空列表表示纯对话。"""
        return [item.function_schema() for item in definitions]


def builtin_tools() -> list[BaseTool]:
    """5 个内置工具实例（延迟导入：`builtin` 包 import 本模块的 id 表）。"""
    from app.runtime.tools.builtin import BUILTIN_TOOLS  # noqa: PLC0415 - 延迟导入，打破 registry ↔ builtin 循环

    return list(BUILTIN_TOOLS)


def default_registry() -> ToolRegistry:
    """生产与测试通用的注册表（5 个内置工具）。"""
    return ToolRegistry(builtin_tools())


def builtin_definitions() -> list[ToolDefinition]:
    """代码侧的 5 条内置工具定义（按 `tools.name` 排序，便于与迁移种子比对）。"""
    definitions = [item.definition(tool_id=builtin_tool_id(item.name)) for item in builtin_tools()]
    return sorted(definitions, key=lambda item: item.name)


def seed_rows() -> list[dict[str, Any]]:
    """迁移 `0003` 的种子行（`tools` 表的列 → 值）；`test_tool_registry.py` 用它锁定契约。"""
    rows: list[dict[str, Any]] = []
    for definition in builtin_definitions():
        rows.append(
            {
                "id": definition.id,
                "name": definition.name,
                "display_name": definition.display_name,
                "description": definition.description,
                "tool_type": definition.tool_type,
                "status": definition.status,
                "input_schema": dict(definition.input_schema),
                "output_schema": dict(definition.output_schema),
                "permission_config": definition.permission_config.model_dump(mode="json"),
                "builtin_name": definition.builtin_name,
                "http_config": dict(definition.http_config),
                "is_system": definition.is_system,
            }
        )
    return rows
