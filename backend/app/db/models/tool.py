"""`tools` / `tool_invocations`（详细设计 2.5）。

- `tools`：工具定义行。内置工具**单一来源是代码**（`runtime/tools/registry.py`），迁移
  `0003_phase2_tool_tables` 的数据迁移把同一份定义写进本表（4.2.2：迁移后即可用）；
  `tests/unit/test_tool_registry.py` 逐字段断言"迁移种子 == 代码定义"。
- `tool_invocations`：每次调用的结构化明细（2.5：Trace 落库后仍需可 SQL 查询的审计行）。
  行由服务层的 `ToolInvocationSink` 写入 —— `runtime/**` 不 import 本模块（1.2）。
- `timeout_seconds` **不单独建列**：它属于 `permission_config`（2.5 的完整结构，
  `ToolPermissionConfig` 已含该字段）；`api` 类型另有 `http_config.timeout_seconds`
  （请求级超时，语义不同，2.5 的 `http_config` 结构）。

三处刻意的"只建列不建 FK"（被引用表在后继迭代才出现，避免悬空外键）：

- `tools.mcp_server_id` → `mcp_servers.id`（迭代 B，SD-16）；
- `tools.mcp_tool_name` 为 MCP 侧原始工具名（迭代 B，SD-16）；
- `tool_invocations.approval_id` → `approvals.id`（迭代 C，SD-17：MVP 恒为 NULL）。

`tool_invocations.tool_id` 用 `ON DELETE SET NULL`：2.5 明确 `tool_name` 是冗余列，
"便于工具删除后追溯" —— 删工具不能连带删掉历史调用记录。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import PermissionDecision, RunStatus
from app.db.base import Base, JSONDict, JSONList, TimestampMixin, UUIDStr, now_utc

TOOL_STATUS_ENABLED = "enabled"
TOOL_STATUS_DISABLED = "disabled"

TOOL_TYPE_BUILTIN = "builtin"
TOOL_TYPE_API = "api"

DEFAULT_PERMISSION_DECISION = PermissionDecision.ALLOW
"""`tool_invocations.permission_decision` 的列默认值（2.5）。"""


class Tool(Base, TimestampMixin):
    """工具定义（2.5 的全量列）。"""

    __tablename__ = "tools"

    id: Mapped[UUIDStr]
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    """暴露给 LLM 的函数名（`snake_case`）；UNIQUE 也是"内置工具与 `api` 工具不重名"的约束。"""
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    """**直接进入 function schema**（4.2.2），需精写。"""
    tool_type: Mapped[str] = mapped_column(String(16), default=TOOL_TYPE_BUILTIN, nullable=False)
    input_schema: Mapped[JSONDict]
    """JSON Schema（draft-07 子集）；`runtime/tools/base.py::validate_arguments` 只支持该子集。"""
    output_schema: Mapped[JSONDict]
    """仅文档 / 校验用，不强制（2.5）。"""
    builtin_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """`tool_type=builtin` 时必填，且必须已注册在 `ToolRegistry`（4.2.3 步骤 1）。"""
    http_config: Mapped[JSONDict | None] = mapped_column(JSON, nullable=True)
    """`tool_type=api` 的请求配置（`method` / `url` / `headers` / `query` / `body_template` …）。"""
    mcp_server_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    mcp_tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    permission_config: Mapped[JSONDict] = mapped_column(JSON, default=dict)
    """`ToolPermissionConfig` 的 JSON 形态（`level` / `require_approval` / 白名单 / 限额 …）。"""
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """内置工具：禁删、可禁用 / 可改权限（2.5）。"""
    status: Mapped[str] = mapped_column(String(16), default=TOOL_STATUS_ENABLED, nullable=False)
    tags: Mapped[JSONList]


class ToolInvocation(Base):
    """一次工具调用的明细（2.5）；`status` 只用 `RunStatus` 的 succeeded / failed / canceled。"""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tool_invocations_run_id", "run_id"),
        Index("ix_tool_invocations_tool_created", "tool_id", "created_at"),
    )

    id: Mapped[UUIDStr]
    run_id: Mapped[str] = mapped_column(String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """该 Run 内第几次 LLM↔Tool 循环（4.4.3）。"""
    call_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """同一 step 内的第几个 tool call（SD-2：顺序执行，语义最简单）。"""
    tool_id: Mapped[str | None] = mapped_column(String(26), ForeignKey("tools.id", ondelete="SET NULL"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    """冗余列：工具被删除后仍能追溯（2.5）。"""
    arguments: Mapped[JSONDict]
    """LLM 给出的原始参数（未清洗，含 `raw_arguments` 的解析结果）。"""
    normalized_arguments: Mapped[JSONDict]
    """经 schema 校验 / 默认值填充后的参数。"""
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    """结果文本（截断至 `max_output_bytes`；dict 结果已归一为 JSON 文本）。"""
    result_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.SUCCEEDED, nullable=False)
    permission_decision: Mapped[str] = mapped_column(String(16), default=DEFAULT_PERMISSION_DECISION, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
