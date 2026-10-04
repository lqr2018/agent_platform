"""工具 DTO（详细设计 3.2.3 / 2.5）。

`mcp_server_id` / `mcp_tool_name` 属 Backlog 迭代 B（SD-16），MVP 恒为 NULL → 不外露；
`http_config` 可能含请求头等敏感配置，只在详情（`ToolDetail`）里返回。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import UtcDatetime


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
