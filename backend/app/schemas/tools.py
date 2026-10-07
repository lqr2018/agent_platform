"""工具 DTO（详细设计 3.2.3 / 2.5）。

`mcp_server_id` / `mcp_tool_name` 属 Backlog 迭代 B（SD-16），MVP 恒为 NULL → 不外露；
`http_config` 可能含请求头等敏感配置，只在详情（`ToolDetail`）里返回。

**请求侧**（`ToolCreate` / `ToolUpdate` / `ToolTestRequest`）与**响应侧**（`ToolRead` /
`ToolDetail` / `ToolInvocationRead`）刻意分开：

- 响应里 `permission_config` 保持 `dict`（DB 里的原样，含运营侧只改了部分键的历史行）；
- 请求里 `permission_config` 用 `ToolPermissionConfigDTO` —— 与 runtime 的
  `ToolPermissionConfig` 字段一一对应（`tests/unit/test_tool_schemas.py` 逐字段断言两份副本），
  但**不 import runtime**：`api` 层不直接依赖 `runtime`（1.2），由服务层完成 DTO → runtime 的转换。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import PermissionLevel
from app.schemas.common import UtcDatetime

ToolStatus = Literal["enabled", "disabled"]
"""2.5：`tools.status` 的两个取值。"""

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
"""`tools.name` 是暴露给 LLM 的函数名（2.5）：小写 `snake_case`、最长 64 字符。"""

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
"""`api` 工具允许的 `http_config.method`（2.5 的 `http_config` 结构）。"""


class ToolRead(BaseModel):
    """`tools` 行（2.5，前端工具管理页与 Chat 工具卡片的展示字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    display_name: str = ""
    description: str = ""
    tool_type: str = "builtin"
    status: str = "enabled"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    builtin_name: str | None = None
    permission_config: dict[str, Any] = Field(default_factory=dict)
    is_system: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ToolDetail(ToolRead):
    """工具详情：附加 `api` 类型的 `http_config`（2.5）。"""

    http_config: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationRead(BaseModel):
    """`tool_invocations` 行（2.5）：每次调用的结构化明细（审计 / 前端工具卡片）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    trace_id: str = ""
    span_id: str = ""
    step_index: int = 0
    call_index: int = 0
    tool_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    normalized_arguments: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    result_truncated: bool = False
    status: str = "succeeded"
    permission_decision: str = "allow"
    approval_id: str | None = None
    """SD-17：MVP 恒为 `None`（审批属 Backlog 迭代 C）。"""
    attempt: int = 1
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0
    created_at: UtcDatetime


class ToolPermissionConfigDTO(BaseModel):
    """`permission_config` 的**请求侧**形态（2.5 的完整结构）。

    字段与 runtime 的 `ToolPermissionConfig` 一一对应（由 `tests/unit/test_tool_schemas.py` 钉住）；
    `extra="ignore"` 让后加的运行时字段不会让旧客户端请求 422。
    """

    model_config = ConfigDict(extra="ignore")

    level: PermissionLevel = PermissionLevel.SAFE
    require_approval: bool = False
    allowed_paths: list[str] = Field(default_factory=list)
    allow_network: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)
    max_output_bytes: int = Field(default=65_536, ge=1)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_calls_per_run: int = Field(default=20, ge=1)


class ToolCreate(BaseModel):
    """`POST /tools`（3.2.3）：**只建 `api` 类型**。

    `builtin` 工具由迁移 `0003` + 启动对齐维护、`mcp` 属 Backlog 迭代 B（SD-16），
    因此 `tool_type` 用 `Literal["api"]` 收口（其余取值由 Pydantic 直接 422）。
    """

    name: str = Field(min_length=1, max_length=64, pattern=TOOL_NAME_PATTERN)
    display_name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4_000)
    tool_type: Literal["api"] = "api"
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    http_config: dict[str, Any] = Field(default_factory=dict)
    permission_config: ToolPermissionConfigDTO = Field(default_factory=ToolPermissionConfigDTO)
    status: ToolStatus = "enabled"
    tags: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        """先剥空白再跑 `pattern`（`mode="before"`：`"  name  "` 不该因为空格被判非法）。"""
        return value.strip() if isinstance(value, str) else value


class ToolUpdate(BaseModel):
    """`PATCH /tools/{id}`：只传需要改的字段（同 `ProviderUpdate` 的约定）。

    内置（`is_system=true`）行只接受 `status` / `permission_config` / `tags`
    —— 其余列是**代码定义**（4.2.2 的单一来源），改了也会被启动对齐覆盖（服务层给出 422）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=TOOL_NAME_PATTERN)
    display_name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4_000)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    http_config: dict[str, Any] | None = None
    permission_config: ToolPermissionConfigDTO | None = None
    status: ToolStatus | None = None
    tags: list[str] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ToolTestRequest(BaseModel):
    """`POST /tools/{id}/test` 的请求（3.2.3：直接执行一次，跳过 LLM）。"""

    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolTestResult(BaseModel):
    """`POST /tools/{id}/test` 的结果。

    与 `ProviderTestResult` 同风格：**执行失败也是 HTTP 200 + `ok=false`**（`3.2.2` 的先例），
    因为这是"运营试跑"而不是业务调用 —— 调用侧的失败语义由 `tool_invocations` 承载。
    """

    ok: bool
    tool_id: str
    tool_name: str
    status: str = "succeeded"
    permission_decision: str = "allow"
    latency_ms: int = 0
    result: str | None = None
    truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    """九步流水线的失败细节（`reason` / `tool_name` / `permission_level` …），便于页面直接展示。"""
