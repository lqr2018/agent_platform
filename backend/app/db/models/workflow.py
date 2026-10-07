"""`workflows` / `workflow_runs` / `node_runs`（详细设计 2.9 / 7.4）。

三张表的关系：

```text
workflows ──< workflow_runs ──< node_runs
   ▲                                │
   └── agents.workflow_id           └── agent_run_id → runs.id（4.8.1 的单一 trace 树要求
                                        node → agent 共享同一条 `runs`，见附录 F v1.13）
```

- `definition` 是图的**唯一事实来源**（2.9）：节点 + 边 + `config`；`start_node_id` 冗余存一份便于
  校验与列表展示（写入时由服务层解析 `definition` 得到，不接受客户端直接指定）；
- `workflow_runs.definition_snapshot` 是运行时的图快照（保证回放一致，2.9）；
- `checkpoint` 与引擎无关（`engine` / `engine_version` / `state` / `current_node_id`，4.5.4，SD-10）；
- `node_runs.seq` 是 3.2.7 的排序键（`GET /workflow-runs/{id}/node-runs` 按 `seq` 返回），
  2.9 的表结构漏列，已在附录 F v1.13 记为"按接口契约补列"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import NodeType, RunStatus, WorkflowStatus
from app.db.base import Base, JSONDict, TimestampMixin, UUIDStr, now_utc

WORKFLOW_DEFINITION_VERSION = 1
"""`workflows.version` 的初值（2.9：每次发布 +1）。"""

DEFAULT_WORKFLOW_MAX_STEPS = 30
"""`definition.config.max_steps` 默认值（4.5.2：循环上限）。"""

DEFAULT_WORKFLOW_RECURSION_LIMIT = 50
"""`definition.config.recursion_limit` 默认值（4.5.2）。"""

DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 600
"""`definition.config.timeout_seconds` 默认值（2.9 的示例定义）。"""

TRIGGER_MANUAL = "manual"
TRIGGER_API = "api"
TRIGGER_EVAL = "eval"
"""`workflow_runs.trigger` 的三个取值（2.9）。"""

NODE_RUN_SEQUENCE_START = 1
"""`node_runs.seq` 从 1 开始（与 `spans.seq` 的写法一致）。"""


class Workflow(Base, TimestampMixin):
    """Workflow 定义（2.9）：一个可编排的有向图（**串行**，SD-1）。"""

    __tablename__ = "workflows"

    id: Mapped[UUIDStr]
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=WORKFLOW_DEFINITION_VERSION, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=str(WorkflowStatus.DRAFT), nullable=False)
    definition: Mapped[JSONDict]
    state_schema: Mapped[JSONDict]
    start_node_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class WorkflowRun(Base):
    """Workflow 的一次执行实例（2.9）。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_workflow_started", "workflow_id", "started_at"),
        Index("ix_workflow_runs_status", "status"),
    )

    id: Mapped[UUIDStr]
    workflow_id: Mapped[str] = mapped_column(String(26), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, default=WORKFLOW_DEFINITION_VERSION, nullable=False)
    definition_snapshot: Mapped[JSONDict]
    status: Mapped[str] = mapped_column(String(16), default=str(RunStatus.PENDING), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default=TRIGGER_MANUAL, nullable=False)
    input: Mapped[JSONDict]
    output: Mapped[JSONDict]
    state: Mapped[JSONDict]
    current_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint: Mapped[JSONDict]
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class NodeRun(Base):
    """节点的一次执行记录（2.9）：状态 / 耗时 / 输出摘要 / 重试次数。"""

    __tablename__ = "node_runs"
    __table_args__ = (Index("ix_node_runs_run_seq", "run_id", "seq"),)

    id: Mapped[UUIDStr]
    run_id: Mapped[str] = mapped_column(String(26), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(16), default=str(NodeType.START), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=NODE_RUN_SEQUENCE_START, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    """循环体内的第几次（`condition` 回边，2.9）。"""

    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    """同一节点的第几次尝试（`on_error=retry(n)`，4.5.2）。"""

    status: Mapped[str] = mapped_column(String(16), default=str(RunStatus.RUNNING), nullable=False)
    input: Mapped[JSONDict]
    output: Mapped[JSONDict]
    agent_run_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


def definition_config(definition: dict[str, Any] | None) -> dict[str, Any]:
    """取 `definition.config`（缺省 / 非 dict 时回落到 4.5.2 的默认值）。

    服务层与引擎都通过它读 `max_steps` / `recursion_limit` / `timeout_seconds`，
    避免"同一个默认值写两遍"。
    """
    raw = (definition or {}).get("config")
    config = dict(raw) if isinstance(raw, dict) else {}
    config.setdefault("max_steps", DEFAULT_WORKFLOW_MAX_STEPS)
    config.setdefault("recursion_limit", DEFAULT_WORKFLOW_RECURSION_LIMIT)
    config.setdefault("timeout_seconds", DEFAULT_WORKFLOW_TIMEOUT_SECONDS)
    return config
