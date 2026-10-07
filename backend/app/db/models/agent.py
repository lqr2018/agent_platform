"""`agents` / `agent_prompt_versions`（详细设计 2.4）。

- `model` 拆为 `model_provider_id` + `model_name` + `model_params`（2.4）；
- `memory_config` 的默认值只生效 `short_term`（SD-15）；`long_term` 子字段保留但不生效；
- `workflow_id`：Phase 1 只建列，**Phase 3 已用 batch 迁移补上 FK**（`workflows.id`，`ON DELETE SET NULL`：
  删 Workflow 不应连带删 Agent，Agent 回落到"直接跑 AgentRuntime"的语义，4.4.4）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, JSONList, TimestampMixin, UUIDStr, now_utc

AGENT_STATUS_ENABLED = "enabled"
AGENT_STATUS_DISABLED = "disabled"

DEFAULT_MEMORY_CONFIG: dict[str, object] = {
    "short_term": {"strategy": "window", "max_turns": 12, "max_tokens": 6000, "summarize_threshold_tokens": 4000},
    # long_term 保留字段但不生效（SD-15 / 2.4）
    "long_term": {
        "enabled": False,
        "scope": "agent",
        "retrieve_top_k": 5,
        "min_score": 0.35,
        "write_policy": "auto",
        "ttl_seconds": None,
    },
}


def default_memory_config() -> dict[str, object]:
    """`memory_config` 的列默认值（深拷贝，避免多个实例共享同一个 dict）。"""
    copy: dict[str, object] = json.loads(json.dumps(DEFAULT_MEMORY_CONFIG))
    return copy


class Agent(Base, TimestampMixin):
    """Agent 配置行（2.4 的全量列）。"""

    __tablename__ = "agents"

    id: Mapped[UUIDStr]
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AGENT_STATUS_ENABLED, nullable=False)
    model_provider_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("model_providers.id", ondelete="RESTRICT"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_params: Mapped[JSONDict]
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tool_ids: Mapped[JSONList]
    knowledge_base_ids: Mapped[JSONList]
    memory_config: Mapped[JSONDict] = mapped_column(default=default_memory_config)
    workflow_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("workflows.id", ondelete="SET NULL", name="fk_agents_workflow_id_workflows"),
        nullable=True,
    )
    max_steps: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=180, nullable=False)
    tags: Mapped[JSONList]
    is_template: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentPromptVersion(Base):
    """Prompt 版本快照（2.4：`PATCH /agents/{id}` 改 prompt 时先落旧值）。"""

    __tablename__ = "agent_prompt_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_prompt_versions_agent_version"),)

    id: Mapped[UUIDStr]
    agent_id: Mapped[str] = mapped_column(String(26), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
