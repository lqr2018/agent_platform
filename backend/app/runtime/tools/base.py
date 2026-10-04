"""工具契约层（详细设计 4.2.1 / 4.2.2 / 2.5）。

`runtime/**` 不 import `db/models` / `services`（1.2）：服务层把 `tools` 行装配成
`ToolDefinition` 快照传入，`BaseTool` 只认快照 + `ToolContext`。

本模块还提供 **JSON Schema（draft-07 子集）校验器**（4.2.3 步骤 3 的落点）。
刻意不引入 `jsonschema`：0.2.3 的依赖基线没有它，与 Phase 0/1 "不引入 respx /
OpenAI SDK" 是同一条纪律（能自研的小件不要新依赖）。支持的关键字：

    type / properties / required / additionalProperties / items / enum
    minimum / maximum / minLength / maxLength / minItems / maxItems

`description` / `title` 等纯展示字段被忽略（它们只给 LLM 看，见 4.2.2 的 schema 示例）。
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.enums import PermissionDecision, PermissionLevel, RunStatus, ToolType
from app.runtime.llm.base import ChatMessage
from app.runtime.observability.tracer import Span

LEVEL_ORDER: Mapping[PermissionLevel, int] = {
    PermissionLevel.SAFE: 0,
    PermissionLevel.GUARDED: 1,
    PermissionLevel.DANGEROUS: 2,
}
"""`safe < guarded < dangerous`：合并时取更大者（4.2.3 步骤 2 的"更严格"）。"""


class ToolPermissionConfig(BaseModel):
    """`tools.permission_config` / `agents` 级权限（2.5 的完整结构）。"""

    model_config = {"extra": "ignore"}

    level: PermissionLevel = PermissionLevel.SAFE
    require_approval: bool = False
    allowed_paths: list[str] = Field(default_factory=list)
    allow_network: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)
    max_output_bytes: int = 65_536
    timeout_seconds: float = 15.0
    max_calls_per_run: int = 20

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ToolPermissionConfig:
        """从 JSON 列（dict）构造；`None` 视为全默认（2.5）。"""
        return cls.model_validate(dict(data or {}))

    def merged_with(self, other: ToolPermissionConfig | None = None) -> ToolPermissionConfig:
        """Agent 级与工具级取**更严格者**（4.2.3 步骤 2）。

        规则（写死，由 `tests/unit/test_tool_permissions.py` 锁定）：

        - `level`：取更高一级（`safe < guarded < dangerous`）；
        - `require_approval`：`or`（任一方要求审批即要求）；
        - `allow_network`：`and`（任一方禁止即禁止）；
        - `allowed_paths` / `allowed_hosts`：**交集**；一方为空表示"不额外限制"，取另一方；
        - 数值项（`max_output_bytes` / `timeout_seconds` / `max_calls_per_run`）：取更小者。
        """
        if other is None:
            return self
        return ToolPermissionConfig(
            level=max((self.level, other.level), key=lambda item: LEVEL_ORDER[item]),
            require_approval=self.require_approval or other.require_approval,
            allowed_paths=_intersect_paths(self.allowed_paths, other.allowed_paths),
            allow_network=self.allow_network and other.allow_network,
            allowed_hosts=_intersect_values(self.allowed_hosts, other.allowed_hosts),
            max_output_bytes=min(self.max_output_bytes, other.max_output_bytes),
            timeout_seconds=min(self.timeout_seconds, other.timeout_seconds),
            max_calls_per_run=min(self.max_calls_per_run, other.max_calls_per_run),
        )


def _intersect_values(left: Sequence[str], right: Sequence[str]) -> list[str]:
    """白名单交集；一方为空 = 不额外限制 → 取另一方（4.2.3 步骤 2）。"""
    if not left:
        return list(right)
    if not right:
        return list(left)
    return [item for item in left if item in set(right)]


def _intersect_paths(left: Sequence[str], right: Sequence[str]) -> list[str]:
    """路径白名单：与前缀语义兼容（`uploads/` 覆盖 `uploads/a.txt`）。"""
    if not left:
        return list(right)
    if not right:
        return list(left)
    result: list[str] = []
    for item in left:
        for other in right:
            if item.startswith(other) or other.startswith(item):
                result.append(item if len(item) <= len(other) else other)
    return sorted(set(result))


# ---- JSON Schema（draft-07 子集）校验：4.2.3 步骤 3 ----
def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """校验 `arguments`；返回错误消息列表（空列表 = 通过）。

    错误消息面向 LLM 回填（4.2.3 步骤 3 / 2524 行的风险对策）：形如
    `"expression": expected string, got integer`。
    """
    errors: list[str] = []
    _validate_value(schema, arguments, path="", errors=errors)
    return errors


def normalize_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """按 schema 填充默认值后的参数（落 `tool_invocations.normalized_arguments`，2.5）。"""
    properties = schema.get("properties")
    normalized = dict(arguments)
    if isinstance(properties, Mapping):
        for name, spec in properties.items():
            if name not in normalized and isinstance(spec, Mapping) and "default" in spec:
                normalized[name] = spec["default"]
    return normalized


def schema_digest(schema: Mapping[str, Any]) -> dict[str, Any]:
    """回填给 LLM 的"期望结构"摘要（只留 type / required / 字段名，避免整段 schema 太长）。"""
    properties = schema.get("properties")
    fields: list[str] = []
    if isinstance(properties, Mapping):
        for name, spec in properties.items():
            expected = spec.get("type") if isinstance(spec, Mapping) else None
            fields.append(f"{name}:{expected}" if expected else str(name))
    return {"type": schema.get("type", "object"), "required": list(schema.get("required") or []), "fields": fields}


def _validate_value(schema: Mapping[str, Any], value: Any, *, path: str, errors: list[str]) -> None:
    if not isinstance(schema, Mapping):
        return

    expected = schema.get("type")
    if expected is not None and not _matches_type(expected, value):
        errors.append(f"{_label(path)}: expected {expected}, got {_type_name(value)}")
        return

    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, str) and value not in enum:
        errors.append(f"{_label(path)}: expected one of {list(enum)}, got {value!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{_label(path)}: must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{_label(path)}: must be <= {maximum}")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{_label(path)}: must be at least {min_length} characters")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{_label(path)}: must be at most {max_length} characters")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{_label(path)}: must have at least {min_items} items")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{_label(path)}: must have at most {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_value(item_schema, item, path=f"{path}[{index}]", errors=errors)

    if isinstance(value, Mapping):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{_label(str(name))}: required property is missing")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in properties:
                _validate_value(properties[key], item, path=child_path, errors=errors)
            elif additional is False:
                errors.append(f"{_label(child_path)}: unexpected property")
            elif isinstance(additional, Mapping):
                _validate_value(additional, item, path=child_path, errors=errors)


def _matches_type(expected: str | Sequence[str], value: Any) -> bool:
    types = [expected] if isinstance(expected, str) else list(expected)
    return any(_matches_single_type(item, value) for item in types)


_SIMPLE_TYPE_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}
"""除 `type` 之外的单类型判定表；未知类型一律放行（宽松策略，避免误杀工具参数）。"""


def _matches_single_type(expected: str, value: Any) -> bool:
    check = _SIMPLE_TYPE_CHECKS.get(expected)
    return True if check is None else check(value)


_TYPE_NAMES: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
)


def _type_name(value: Any) -> str:
    """把 Python 值映射回 JSON Schema 的类型名（只用于拼错误信息）。"""
    if value is None:
        return "null"
    for python_type, name in _TYPE_NAMES:
        if isinstance(value, python_type):
            return name
    if isinstance(value, Mapping):
        return "object"
    return "array" if isinstance(value, list) else type(value).__name__


def _label(path: str) -> str:
    return f'"{path}"' if path else "arguments"


# ---- 工具状态 / 类型常量（`mcp` 只用于识别并拒绝，进不了 0.2.2 A 段枚举） ----
TOOL_STATUS_ENABLED = "enabled"
TOOL_STATUS_DISABLED = "disabled"
TOOL_TYPE_MCP = "mcp"
"""MVP 不接受 `tool_type=mcp`（SD-16）：4.2.3 步骤 1 用它判定 `TOOL_TYPE_NOT_SUPPORTED`。"""


# ---- 运行时上下文与结果（4.2.1） ----
@dataclass(slots=True)
class ToolContext:
    """一次工具执行的上下文（4.2.1 的 `ToolContext` + 合并后的权限投影）。"""

    run_id: str
    trace_span: Span
    sandbox_root: Path
    settings: Settings
    cancellation: asyncio.Event
    allowed_paths: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allow_network: bool = False
    max_output_bytes: int = 65_536
    timeout_seconds: float = 15.0


@dataclass(slots=True)
class ToolResult:
    """工具返回值（4.2.1）：`content` 回填给 LLM，`meta` 合入 `span.attributes`。"""

    content: str | dict[str, Any]
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        """归一为文本（dict 走 JSON，对应 2.5 的 `result TEXT/JSON`）。"""
        if isinstance(self.content, str):
            return self.content
        return json.dumps(self.content, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """`tools` 行的运行时快照（1.2：runtime 不 import ORM）。"""

    id: str
    name: str
    display_name: str = ""
    description: str = ""
    tool_type: str = "builtin"
    status: str = TOOL_STATUS_ENABLED
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    builtin_name: str | None = None
    http_config: Mapping[str, Any] = field(default_factory=dict)
    """`api` 类型的请求配置（2.5：`method` / `url` / `headers` / `query` / `body_template` …）。"""
    permission_config: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)
    is_system: bool = False
    tags: tuple[str, ...] = ()

    @property
    def is_enabled(self) -> bool:
        return self.status == TOOL_STATUS_ENABLED

    @property
    def is_builtin(self) -> bool:
        return self.tool_type == ToolType.BUILTIN

    @property
    def is_mcp(self) -> bool:
        return self.tool_type == TOOL_TYPE_MCP

    def function_schema(self) -> dict[str, Any]:
        """OpenAI function schema（4.2.2）：`description` 直接进 schema，需精写（2.5）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema or {"type": "object", "properties": {}}),
            },
        }


class BaseTool(ABC):
    """内置工具基类（4.2.1）。子类只声明元信息与 `run()`。"""

    name: str = ""
    display_name: str = ""
    description: str = ""
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    output_schema: Mapping[str, Any] = {}
    default_permission = ToolPermissionConfig()

    @abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """执行工具（`kwargs` 为已通过 schema 校验的参数）。"""

    def definition(self, *, tool_id: str, status: str = TOOL_STATUS_ENABLED, is_system: bool = True) -> ToolDefinition:
        """代码侧定义 → 与 DB 行等价的快照（4.2.2：内置工具定义以代码为单一来源）。"""
        return ToolDefinition(
            id=tool_id,
            name=self.name,
            display_name=self.display_name or self.name,
            description=self.description,
            tool_type=str(ToolType.BUILTIN),
            status=status,
            input_schema=dict(self.input_schema),
            output_schema=dict(self.output_schema),
            builtin_name=self.name,
            permission_config=self.default_permission,
            is_system=is_system,
        )


@dataclass(slots=True)
class ToolInvocationResult:
    """一次工具调用的结果（4.2.3 步骤 9：供 `AgentRuntime` 组装 `role=tool` 消息）。"""

    tool_call_id: str
    tool_name: str
    tool_id: str | None = None
    status: RunStatus = RunStatus.SUCCEEDED
    content: str = ""
    is_error: bool = False
    error_code: str | None = None
    error_message: str | None = None
    permission_decision: PermissionDecision = PermissionDecision.ALLOW
    arguments: dict[str, Any] = field(default_factory=dict)
    normalized_arguments: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    latency_ms: int = 0
    step_index: int = 0
    call_index: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == RunStatus.SUCCEEDED

    def to_message(self) -> ChatMessage:
        """回填给 LLM 的 `role=tool` 消息（4.4.3：`assistant(tool_calls)` + `tool` 交替）。"""
        return ChatMessage.tool(self.content, tool_call_id=self.tool_call_id, name=self.tool_name)
