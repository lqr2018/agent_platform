"""上下文装配（详细设计 4.4.2 的顺序，**写死**）。

```text
1. system 消息 = agent.system_prompt（+ Phase 5 的检索结果块）
2. 短期记忆    = ShortTermMemory.build()（window 策略，4.3.1）
3. 本次 user 消息
4. 循环中的 assistant(tool_calls) 与 tool 消息（按 step 追加，Phase 2）
```

每一步都**重新组装**（工具结果进入 messages），历史消息本身不被修改，保证可重放（4.4.2）。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.runtime.agent.prompt import build_system_message
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage
from app.runtime.memory.base import ShortTermMemory


async def assemble_context(
    *,
    agent: AgentSpec,
    memory: ShortTermMemory,
    conversation_id: str | None,
    user_input: str,
    before_seq: int | None = None,
    retrieved_context: Sequence[str] | None = None,
) -> list[ChatMessage]:
    """按 4.4.2 的顺序装配发给 LLM 的上下文。

    `before_seq` 用于排除"本轮刚落库的 user 消息"（文档 4.3.1 的签名细化），
    避免同一条 user 消息出现两次。
    """
    messages: list[ChatMessage] = [build_system_message(agent, retrieved_context=retrieved_context)]
    if conversation_id:
        history = await memory.build(conversation_id, agent=agent, before_seq=before_seq)
        messages.extend(history)
    messages.append(ChatMessage.user(user_input))
    return messages
