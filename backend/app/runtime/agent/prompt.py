"""system 提示词装配（详细设计 4.4.2 第 1 步）。

- Phase 1：`system = agent.system_prompt`；
- Phase 5 追加"以下是知识库检索结果…"块（`retrieved_context` 参数已就位）；
- 长期记忆注入属 Backlog（SD-15），MVP **不注入**（4.4.2 的明确说明）。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
"""未配置 prompt 时的兜底（保证 messages[0] 始终是 system，4.4.2 写死的顺序）。"""

RETRIEVAL_BLOCK_HEADER = "以下是知识库检索结果，回答时请引用来源："
RETRIEVAL_BLOCK_TEMPLATE = '<chunk id="{chunk_id}" source="{source}">\n{content}\n</chunk>'


def build_system_message(
    agent: AgentSpec,
    *,
    retrieved_context: Sequence[str] | None = None,
) -> ChatMessage:
    """装配 system 消息（Phase 5 的检索块在 prompt 之后追加）。"""
    parts = [agent.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT]
    if retrieved_context:
        parts.append(RETRIEVAL_BLOCK_HEADER)
        parts.extend(retrieved_context)
    return ChatMessage.system("\n\n".join(parts))


def format_retrieved_chunk(*, content: str, chunk_id: str = "", source: str = "") -> str:
    """把一条检索结果渲染为 4.4.2 约定的 `<chunk …>` 块（Phase 5 使用）。"""
    return RETRIEVAL_BLOCK_TEMPLATE.format(chunk_id=chunk_id, source=source, content=content.strip())
