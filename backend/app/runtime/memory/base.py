"""记忆抽象（详细设计 4.3.1 / 4.3.3）。

MVP 只有**短期记忆**（= 上下文窗口装配，无独立存储）；长期记忆（SD-15）不在本阶段代码里。
runtime 不碰 ORM：历史消息通过 `MessageSource` 协议由服务层实现（`conversation_service`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.core.enums import MessageRole
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage, ToolCallSpec
from app.runtime.llm.usage import TokenUsage


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """`messages` 表的一行在运行时的最小投影（4.3.1）。"""

    seq: int
    role: MessageRole
    content: str = ""
    message_id: str | None = None
    tool_calls: tuple[ToolCallSpec, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def to_chat_message(self) -> ChatMessage:
        return ChatMessage(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            name=self.name,
        )


class MessageSource(Protocol):
    """历史消息读取（服务层用 ORM 实现；runtime 只依赖协议）。"""

    async def recent_messages(
        self, conversation_id: str, *, limit: int, before_seq: int | None = None
    ) -> list[StoredMessage]: ...


class ShortTermMemory(Protocol):
    """4.3.1 的接口（含 Phase 1 的三处签名细化，详见文档 4.3.1 的说明块）。"""

    async def build(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        before_seq: int | None = None,
    ) -> list[ChatMessage]: ...

    async def append(self, conversation_id: str, message: ChatMessage, *, run_id: str | None = None) -> str: ...

    async def complete(
        self,
        message_id: str,
        *,
        content: str,
        finish_reason: str,
        usage: TokenUsage,
        latency_ms: int,
        model_name: str,
        error_code: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """`memory_config` 的读侧投影（MVP 只读 `short_term`，SD-15）。"""

    short_term: Mapping[str, object] = field(default_factory=dict)
    long_term: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_agent(cls, agent: AgentSpec) -> MemoryConfig:
        raw = dict(agent.memory_config)
        short_term = raw.get("short_term")
        long_term = raw.get("long_term")
        return cls(
            short_term=short_term if isinstance(short_term, Mapping) else {},
            long_term=long_term if isinstance(long_term, Mapping) else {},
        )
