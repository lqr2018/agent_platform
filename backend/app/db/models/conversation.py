"""`conversations` / `messages` / `runs`（详细设计 2.6）。

- `messages` 的 `(conversation_id, seq)` UNIQUE：既是会话回放顺序，也是幂等兜底（1.5.5）；
- `runs` 是统一执行实例（chat / workflow / eval），Chat 每轮消息一个 Run；
- `runs` 行在 Run 开始时即插入（`status=running`），崩溃后能查到"孤儿 Run"（2.6）；
- `workflow_run_id` / `eval_result_id`：Phase 1 只建列；`workflow_run_id` 的 FK 在 Phase 3 用 batch 迁移补上，
  `eval_result_id` 仍留待 Backlog 迭代 D（SD-18）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import MessageRole, RunKind, RunStatus
from app.db.base import Base, JSONDict, JSONList, TimestampMixin, UUIDStr, now_utc

CONVERSATION_STATUS_ACTIVE = "active"
CONVERSATION_STATUS_ARCHIVED = "archived"

OWNER_KEY_LOCAL = "local"
"""SD-3：不做多用户，`owner_key` 固定 `local`。"""

TITLE_MAX_CHARS = 30
"""会话标题默认取首条用户消息前 30 字（2.6）。"""


class Conversation(Base, TimestampMixin):
    """会话（2.6）。"""

    __tablename__ = "conversations"

    id: Mapped[UUIDStr]
    agent_id: Mapped[str] = mapped_column(String(26), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    owner_key: Mapped[str] = mapped_column(String(64), default=OWNER_KEY_LOCAL, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=CONVERSATION_STATUS_ACTIVE, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base):
    """一次执行实例（2.6）：Chat 一轮消息 / Workflow 节点 / 评测用例。"""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_agent_started", "agent_id", "started_at"),)

    id: Mapped[UUIDStr]
    kind: Mapped[str] = mapped_column(String(16), default=RunKind.CHAT, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("workflow_runs.id", ondelete="SET NULL", name="fk_runs_workflow_run_id_workflow_runs"),
        nullable=True,
    )
    eval_result_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.PENDING, nullable=False)
    input: Mapped[JSONDict]
    output: Mapped[JSONDict]
    steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), nullable=False
    )  # 估算值（4.8.3）
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class Message(Base):
    """消息（2.6）：`role=tool` 行的 `tool_call_id` 与 `name` 供 Tool Calling 还原上下文。"""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),)

    id: Mapped[UUIDStr]
    conversation_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String(26), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=MessageRole.USER, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tool_calls: Mapped[JSONList]
    tool_call_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[JSONDict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


def message_meta(*, agent_prompt_version: int, tools: list[Any], kb_ids: list[Any]) -> dict[str, Any]:
    """`messages.meta` 的固定结构（2.6：用于复现该轮上下文）。"""
    return {"agent_prompt_version": agent_prompt_version, "tools": list(tools), "kb_ids": list(kb_ids)}
