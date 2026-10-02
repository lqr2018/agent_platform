"""短期记忆 = 上下文窗口装配（详细设计 4.3.1，MVP 唯一实现）。

`window` 策略：

1. 取最近 `max_turns` 轮（一轮 = user + 其后所有 assistant/tool 消息）；
2. 再按 `max_tokens` 从后往前裁剪（`estimate_tokens` 粗估）；
3. **裁剪必须成对保留** `assistant(tool_calls)` 与其 `tool(tool_call_id)`，否则部分模型会 400（4.3.1）。

`summary` / `window+summary` 属 Backlog 迭代 A（`long_term.py` 同样如此，SD-15）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.enums import MessageRole
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage
from app.runtime.llm.usage import estimate_tokens
from app.runtime.memory.base import MemoryConfig, MessageSource, StoredMessage

DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOKENS = 6000
FETCH_TURN_MULTIPLIER = 3
"""读取时按"轮数 × 3 行"预估，避免一次拉全表（user + assistant + tool 各一行）。"""


@dataclass(frozen=True, slots=True)
class ShortTermConfig:
    """`memory_config.short_term` 的强类型投影（2.4）。"""

    strategy: str = "window"
    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> ShortTermConfig:
        return cls(
            strategy=str(config.get("strategy") or "window"),
            max_turns=_positive_int(config.get("max_turns"), DEFAULT_MAX_TURNS),
            max_tokens=_positive_int(config.get("max_tokens"), DEFAULT_MAX_TOKENS),
        )


class WindowShortTermMemory:
    """4.3.1 的 `window` 实现：读取 → 分轮 → 裁剪 → 转 `ChatMessage`。"""

    def __init__(self, source: MessageSource) -> None:
        self._source = source

    async def build(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        before_seq: int | None = None,
    ) -> list[ChatMessage]:
        config = ShortTermConfig.from_config(MemoryConfig.from_agent(agent).short_term)
        stored = await self._source.recent_messages(
            conversation_id,
            limit=config.max_turns * FETCH_TURN_MULTIPLIER,
            before_seq=before_seq,
        )
        trimmed = trim_window(stored, max_turns=config.max_turns, max_tokens=config.max_tokens)
        return [item.to_chat_message() for item in trimmed]


def trim_window(stored: Sequence[StoredMessage], *, max_turns: int, max_tokens: int) -> list[StoredMessage]:
    """4.3.1 的裁剪（纯函数，便于单测）：先按轮数截，再按 token 预算从后往前截。"""
    ordered = sorted(stored, key=lambda item: item.seq)
    turns = _split_turns(ordered)
    window = [message for turn in turns[-max_turns:] for message in turn] if max_turns > 0 else []
    return _trim_by_tokens(window, max_tokens)


def _split_turns(messages: Sequence[StoredMessage]) -> list[list[StoredMessage]]:
    """切轮：`user` 起一轮，其后的 `assistant` / `tool` 属同一轮。"""
    turns: list[list[StoredMessage]] = []
    for message in messages:
        if message.role == MessageRole.USER or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _trim_by_tokens(messages: Sequence[StoredMessage], max_tokens: int) -> list[StoredMessage]:
    """从后往前累加 token；随后向前补齐"悬空的 tool 结果"所需的 assistant 消息。"""
    if max_tokens <= 0:
        return list(messages)
    kept: list[StoredMessage] = []
    used = 0
    for message in reversed(messages):
        cost = _message_tokens(message)
        if kept and used + cost > max_tokens:
            break
        kept.append(message)
        used += cost
    kept.reverse()
    return _repair_tool_pairs(kept, messages)


def _repair_tool_pairs(kept: list[StoredMessage], all_messages: Sequence[StoredMessage]) -> list[StoredMessage]:
    """4.3.1：`assistant(tool_calls)` 与 `tool(tool_call_id)` 必须成对保留。

    两步：① 结果不全的 assistant 直接丢弃；② 结果被保留但 assistant 被裁掉的，把 assistant 补回窗口头部。
    """
    if not kept:
        return kept
    kept_result_ids = {item.tool_call_id for item in kept if item.role == MessageRole.TOOL and item.tool_call_id}
    paired: list[StoredMessage] = []
    for item in kept:
        if item.role == MessageRole.ASSISTANT and item.tool_calls:
            call_ids = {call.id for call in item.tool_calls}
            if not call_ids <= kept_result_ids:
                continue
        paired.append(item)

    present_call_ids = {call.id for item in paired if item.role == MessageRole.ASSISTANT for call in item.tool_calls}
    tool_result_ids = {item.tool_call_id for item in paired if item.role == MessageRole.TOOL and item.tool_call_id}
    missing = tool_result_ids - present_call_ids
    prefixes: list[StoredMessage] = []
    if missing:
        first_seq = min((item.seq for item in paired), default=0)
        candidates = [
            item
            for item in all_messages
            if item.seq < first_seq and item.role == MessageRole.ASSISTANT and item.tool_calls
        ]
        for candidate in reversed(candidates):
            matched = {call.id for call in candidate.tool_calls} & missing
            if matched:
                prefixes.append(candidate)
                missing -= matched
            if not missing:
                break
        prefixes.reverse()

    window = [*prefixes, *paired]
    available_call_ids = {call.id for item in window if item.role == MessageRole.ASSISTANT for call in item.tool_calls}
    # 兜底：仍然悬空的 tool 结果（连 assistant 都取不到）只能丢弃，否则模型会 400
    return [
        item
        for item in window
        if not (item.role == MessageRole.TOOL and item.tool_call_id and item.tool_call_id not in available_call_ids)
    ]


def _message_tokens(message: StoredMessage) -> int:
    """一条消息的 token 粗估（正文 + 工具名，不含 schema）。"""
    total = estimate_tokens(message.content)
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(str(call.arguments))
    return max(1, total)


def _positive_int(value: object, default: int) -> int:
    """取正整数；`bool` / 非法值回落到默认值（配置来自 JSON，可能是任意类型）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else default
    return default
