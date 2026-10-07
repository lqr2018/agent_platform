"""进程内短期记忆（无 DB 依赖）：Workflow 手动运行的 `agent` 节点用（7.4）。

Chat 场景用会话表（`conversation_service.SqlShortTermMemory`）；`POST /workflows/{id}/runs`
没有会话，节点产物只进 state 与 `node_runs`（4.5.2），因此这里的消息只活在进程内：

- 每个 `agent` 节点使用**独立实例**（一次"迷你对话"）：节点上下文由 `input_template` 给出，
  不会跨节点互相污染；
- 裁剪复用 `short_term.trim_window`（同一套轮数 / token 规则，4.3.1），避免两处规则漂移；
- `message_id` 由本模块生成（没有 `messages` 行），供 `message.started` / `message.delta` 使用。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.core import ids
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage, ToolCallSpec
from app.runtime.llm.usage import TokenUsage
from app.runtime.memory.base import MemoryConfig, StoredMessage
from app.runtime.memory.short_term import ShortTermConfig, trim_window


class InMemoryShortTermMemory:
    """`ShortTermMemory` 的进程内实现（`build` / `append` / `complete`，4.3.1）。"""

    def __init__(self) -> None:
        self._messages: list[StoredMessage] = []
        self._index_by_id: dict[str, int] = {}

    async def build(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        before_seq: int | None = None,
    ) -> list[ChatMessage]:
        """按 window 策略返回已累积的消息（`conversation_id` 仅作占位，不参与寻址）。"""
        config = ShortTermConfig.from_config(MemoryConfig.from_agent(agent).short_term)
        stored = [item for item in self._messages if before_seq is None or item.seq < before_seq]
        trimmed = trim_window(stored, max_turns=config.max_turns, max_tokens=config.max_tokens)
        return [item.to_chat_message() for item in trimmed]

    async def append(self, conversation_id: str, message: ChatMessage, *, run_id: str | None = None) -> str:
        message_id = ids.new_ulid()
        self._messages.append(
            StoredMessage(
                seq=len(self._messages) + 1,
                role=message.role,
                content=message.content,
                message_id=message_id,
                tool_calls=tuple(message.tool_calls),
                tool_call_id=message.tool_call_id,
                name=message.name,
            )
        )
        self._index_by_id[message_id] = len(self._messages) - 1
        return message_id

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
        tool_calls: Sequence[ToolCallSpec] = (),
    ) -> None:
        """回填占位行（`content` / `tool_calls`）；未知 id 静默忽略（与 SqlShortTermMemory 同语义）。"""
        index = self._index_by_id.get(message_id)
        if index is None:
            return
        current = self._messages[index]
        self._messages[index] = replace(
            current,
            content=content or current.content,
            tool_calls=tuple(tool_calls) or current.tool_calls,
        )
