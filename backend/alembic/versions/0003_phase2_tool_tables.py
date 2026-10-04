"""phase2 tool tables（详细设计 7.2 / 2.5 / 4.2.2 / 4.2.3）

Revision ID: 0003_phase2_tool_tables
Revises: 0002_phase1_core_tables
Create Date: 2026-10-04

Phase 2 建 2 张表（7.2 交付物）：

    tools · tool_invocations

外加**数据迁移**：把 4.2.2 的 5 个内置工具（`calculator` / `file_read` / `file_write` /
`web_search` / `python_execute`）写入 `tools`，让"迁移完成即可用工具"成立（SD-16 的
`approvals` 表**不建**）。

三处刻意的"只建列不建 FK"（被引用表在后继迭代才出现，避免悬空外键）：

- `tools.mcp_server_id` → `mcp_servers.id`（迭代 B，SD-16）；
- `tools.mcp_tool_name`（MCP 侧原始工具名，迭代 B，SD-16）；
- `tool_invocations.approval_id` → `approvals.id`（迭代 C，SD-17：MVP 恒为 NULL）。

`tool_invocations.tool_id` 用 `ON DELETE SET NULL`（2.5：`tool_name` 是冗余列，
"便于工具删除后追溯"，删工具不得连带删掉历史调用）。

`TOOL_SEED_ROWS` 是 `app/runtime/tools/registry.py::seed_rows()` 的**字面副本**：
迁移不 import app 代码，否则代码演进会让历史迁移不可重放；
`tests/unit/test_tool_registry.py` 断言两者逐字段一致。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0003_phase2_tool_tables"
down_revision: str | None = "0002_phase1_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOOL_SEED_ROWS: list[dict[str, Any]] = [
    {
        "builtin_name": "calculator",
        "description": "计算数学表达式并返回结果。只支持算术运算（+ - * / ** // "
        "%）与常用数学函数（sqrt/log/exp/sin/cos/tan/floor/ceil/abs/round/min/max/pow）以及常量 "
        "pi/e/tau；不支持变量、字符串、文件或网络访问。",
        "display_name": "计算器",
        "http_config": {},
        "id": "01J0T00000000000000000001",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "expression": {
                    "description": "要计算的数学表达式，例如 (12+3)*4/sqrt(9)",
                    "maxLength": 500,
                    "minLength": 1,
                    "type": "string",
                }
            },
            "required": ["expression"],
            "type": "object",
        },
        "is_system": True,
        "name": "calculator",
        "output_schema": {"properties": {"result": {"type": "number"}}, "type": "object"},
        "permission_config": {
            "allow_network": False,
            "allowed_hosts": [],
            "allowed_paths": [],
            "level": "safe",
            "max_calls_per_run": 50,
            "max_output_bytes": 65536,
            "require_approval": False,
            "timeout_seconds": 5.0,
        },
        "status": "enabled",
        "tool_type": "builtin",
    },
    {
        "builtin_name": "file_read",
        "description": "读取沙箱目录内的 UTF-8 文本文件。`path` 必须是相对沙箱根目录的相对路径，"
        "例如 `uploads/notes.txt`；越出沙箱或非文本文件会被拒绝。",
        "display_name": "文件读取",
        "http_config": {},
        "id": "01J0T00000000000000000002",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "max_bytes": {
                    "description": "最多返回的字节数（默认取平台配置 TOOL_MAX_OUTPUT_BYTES）",
                    "maximum": 5242880,
                    "minimum": 1,
                    "type": "integer",
                },
                "path": {
                    "description": "相对沙箱根目录的文件路径，例如 uploads/notes.txt",
                    "maxLength": 500,
                    "minLength": 1,
                    "type": "string",
                },
            },
            "required": ["path"],
            "type": "object",
        },
        "is_system": True,
        "name": "file_read",
        "output_schema": {
            "properties": {"content": {"type": "string"}, "path": {"type": "string"}, "truncated": {"type": "boolean"}},
            "type": "object",
        },
        "permission_config": {
            "allow_network": False,
            "allowed_hosts": [],
            "allowed_paths": [],
            "level": "safe",
            "max_calls_per_run": 30,
            "max_output_bytes": 65536,
            "require_approval": False,
            "timeout_seconds": 15.0,
        },
        "status": "enabled",
        "tool_type": "builtin",
    },
    {
        "builtin_name": "file_write",
        "description": "把文本写入沙箱目录下的 `uploads/` 或 `outputs/` 子目录。`mode=append` 追加、"
        "`mode=overwrite` 覆盖；单次写入上限 1 MB。若平台未开启该工具，调用会被拒绝。",
        "display_name": "文件写入",
        "http_config": {},
        "id": "01J0T00000000000000000003",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "content": {"description": "要写入的文本内容", "maxLength": 1048576, "type": "string"},
                "mode": {
                    "default": "append",
                    "description": "append 追加（默认）或 overwrite 覆盖",
                    "enum": ["append", "overwrite"],
                    "type": "string",
                },
                "path": {
                    "description": "相对沙箱根目录的写入路径，必须以 uploads/ 或 outputs/ 开头",
                    "maxLength": 500,
                    "minLength": 1,
                    "type": "string",
                },
            },
            "required": ["path", "content"],
            "type": "object",
        },
        "is_system": True,
        "name": "file_write",
        "output_schema": {
            "properties": {
                "bytes_written": {"type": "integer"},
                "mode": {"type": "string"},
                "path": {"type": "string"},
            },
            "type": "object",
        },
        "permission_config": {
            "allow_network": False,
            "allowed_hosts": [],
            "allowed_paths": [],
            "level": "guarded",
            "max_calls_per_run": 10,
            "max_output_bytes": 65536,
            "require_approval": True,
            "timeout_seconds": 15.0,
        },
        "status": "enabled",
        "tool_type": "builtin",
    },
    {
        "builtin_name": "python_execute",
        "description": "在隔离子进程中执行一小段 Python 代码，返回 exit_code 与 "
        "stdout/stderr。仅允许标准库中的少量模块（math/statistics/json/re/datetime/random/itertools/collections/decimal），禁止文件、网络与系统调用；该工具默认关闭。",
        "display_name": "Python 执行",
        "http_config": {},
        "id": "01J0T00000000000000000005",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "code": {
                    "description": "要执行的 Python 代码（用 print 输出结果）",
                    "maxLength": 4000,
                    "minLength": 1,
                    "type": "string",
                },
                "timeout_seconds": {
                    "description": "超时秒数（默认取平台配置，最多 10 秒）",
                    "maximum": 10.0,
                    "minimum": 0.1,
                    "type": "number",
                },
            },
            "required": ["code"],
            "type": "object",
        },
        "is_system": True,
        "name": "python_execute",
        "output_schema": {
            "properties": {
                "exit_code": {"type": "integer"},
                "stderr": {"type": "string"},
                "stdout": {"type": "string"},
            },
            "type": "object",
        },
        "permission_config": {
            "allow_network": False,
            "allowed_hosts": [],
            "allowed_paths": [],
            "level": "dangerous",
            "max_calls_per_run": 5,
            "max_output_bytes": 65536,
            "require_approval": True,
            "timeout_seconds": 10.0,
        },
        "status": "enabled",
        "tool_type": "builtin",
    },
    {
        "builtin_name": "web_search",
        "description": "调用外部搜索服务检索网页，返回标题、链接与摘要。"
        "适合查询最新事实或训练数据之外的信息；若平台未配置搜索服务，调用会被拒绝。",
        "display_name": "联网搜索",
        "http_config": {},
        "id": "01J0T00000000000000000004",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "max_results": {
                    "default": 5,
                    "description": "返回条数（默认 5，最多 10）",
                    "maximum": 10,
                    "minimum": 1,
                    "type": "integer",
                },
                "query": {
                    "description": "搜索关键词，尽量具体，例如 FastAPI 0.115 release notes",
                    "maxLength": 400,
                    "minLength": 1,
                    "type": "string",
                },
            },
            "required": ["query"],
            "type": "object",
        },
        "is_system": True,
        "name": "web_search",
        "output_schema": {"properties": {"results": {"type": "array"}}, "type": "object"},
        "permission_config": {
            "allow_network": True,
            "allowed_hosts": ["api.tavily.com", "google.serper.dev"],
            "allowed_paths": [],
            "level": "guarded",
            "max_calls_per_run": 10,
            "max_output_bytes": 65536,
            "require_approval": False,
            "timeout_seconds": 20.0,
        },
        "status": "enabled",
        "tool_type": "builtin",
    },
]


def _tools_table() -> sa.TableClause:
    """`bulk_insert` 用的轻量 table 构造（列类型必须给全，JSON 列才会被序列化）。"""
    return sa.table(
        "tools",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("description", sa.Text),
        sa.column("tool_type", sa.String),
        sa.column("input_schema", sa.JSON),
        sa.column("output_schema", sa.JSON),
        sa.column("builtin_name", sa.String),
        sa.column("http_config", sa.JSON),
        sa.column("mcp_server_id", sa.String),
        sa.column("mcp_tool_name", sa.String),
        sa.column("permission_config", sa.JSON),
        sa.column("is_system", sa.Boolean),
        sa.column("status", sa.String),
        sa.column("tags", sa.JSON),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )


def _seed_tool_rows() -> list[dict[str, Any]]:
    """`TOOL_SEED_ROWS` + `tools` 的 DB 级默认列（种子只声明 2.5 的语义字段）。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    return [
        {
            **row,
            "tags": [],
            "mcp_server_id": None,
            "mcp_tool_name": None,
            "created_at": now,
            "updated_at": now,
        }
        for row in TOOL_SEED_ROWS
    ]


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "tools",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tool_type", sa.String(length=16), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("builtin_name", sa.String(length=64), nullable=True),
        sa.Column("http_config", sa.JSON(), nullable=True),
        sa.Column("mcp_server_id", sa.String(length=26), nullable=True),
        sa.Column("mcp_tool_name", sa.String(length=120), nullable=True),
        sa.Column("permission_config", sa.JSON(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("span_id", sa.String(length=16), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(length=26), nullable=True),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("normalized_arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("result_truncated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("permission_decision", sa.String(length=16), nullable=False),
        sa.Column("approval_id", sa.String(length=26), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_id"], ["tools.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tool_invocations", schema=None) as batch_op:
        batch_op.create_index("ix_tool_invocations_run_id", ["run_id"], unique=False)
        batch_op.create_index("ix_tool_invocations_tool_created", ["tool_id", "created_at"], unique=False)

    # 数据迁移：5 个内置工具（4.2.2 的"代码与库对齐"在迁移层的落点）
    op.bulk_insert(_tools_table(), _seed_tool_rows())
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("tool_invocations", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_invocations_tool_created")
        batch_op.drop_index("ix_tool_invocations_run_id")

    op.drop_table("tool_invocations")
    op.drop_table("tools")
    # ### end Alembic commands ###
