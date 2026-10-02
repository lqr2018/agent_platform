"""上下文窗口装配（详细设计 4.3.1：`window` 策略与"成对保留"）。"""

from __future__ import annotations

from app.core.enums import MessageRole
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ToolCallSpec
from app.runtime.memory.base import MemoryConfig, StoredMessage
from app.runtime.memory.short_term import ShortTermConfig, trim_window


def _user(seq: int, content: str) -> StoredMessage:
    return StoredMessage(seq=seq, role=MessageRole.USER, content=content)


def _assistant(seq: int, content: str, calls: tuple[ToolCallSpec, ...] = ()) -> StoredMessage:
    return StoredMessage(seq=seq, role=MessageRole.ASSISTANT, content=content, tool_calls=calls)


def _tool(seq: int, content: str, call_id: str) -> StoredMessage:
    return StoredMessage(seq=seq, role=MessageRole.TOOL, content=content, tool_call_id=call_id)


def test_trim_window_keeps_recent_turns() -> None:
    messages = [_user(1, "第一轮"), _assistant(2, "答 1"), _user(3, "第二轮"), _assistant(4, "答 2")]
    trimmed = trim_window(messages, max_turns=1, max_tokens=10_000)
    assert [item.seq for item in trimmed] == [3, 4]


def test_trim_window_respects_token_budget() -> None:
    messages = [_user(1, "很长的一段历史" * 40), _assistant(2, "同样很长的回答" * 40), _user(3, "最后一个问题")]
    trimmed = trim_window(messages, max_turns=10, max_tokens=20)
    assert [item.seq for item in trimmed] == [3]  # 预算只够最近一条


def test_trim_window_keeps_assistant_and_tool_paired() -> None:
    """4.3.1：裁剪必须成对保留 `assistant(tool_calls)` 与 `tool(tool_call_id)`。"""
    call = ToolCallSpec(id="call_1", name="calculator", arguments={"expression": "1+1"})
    messages = [
        _user(1, "历史"),
        _assistant(2, "我先算一下", (call,)),
        _tool(3, "2", "call_1"),
        _user(4, "谢谢"),
    ]
    trimmed = trim_window(messages, max_turns=10, max_tokens=4)
    seqs = [item.seq for item in trimmed]
    assert 3 in seqs and 2 in seqs, "tool 结果被保留时，其 assistant 必须一起保留"
    assert seqs == sorted(seqs)


def test_trim_window_drops_assistant_without_its_tool_result() -> None:
    call = ToolCallSpec(id="call_9", name="calculator", arguments={})
    messages = [
        _assistant(1, "调用中", (call,)),
        _tool(2, "42", "call_9"),
        _user(3, "继续"),
    ]
    # 预算只够最后一条：assistant+tool 都被裁掉，且不会留下悬空的 tool
    trimmed = trim_window(messages, max_turns=10, max_tokens=2)
    assert [item.seq for item in trimmed] == [3]


def test_short_term_config_from_dict() -> None:
    config = ShortTermConfig.from_config({"strategy": "window", "max_turns": 3, "max_tokens": 500})
    assert (config.strategy, config.max_turns, config.max_tokens) == ("window", 3, 500)
    fallback = ShortTermConfig.from_config({"max_turns": 0, "max_tokens": "oops"})
    assert (fallback.max_turns, fallback.max_tokens) == (12, 6000)


def test_memory_config_reads_short_term_only() -> None:
    agent = AgentSpec(
        id="a1",
        name="agent",
        model_provider_id="p1",
        model_name="m",
        memory_config={"short_term": {"max_turns": 2}, "long_term": {"enabled": True}},
    )
    config = MemoryConfig.from_agent(agent)
    assert config.short_term == {"max_turns": 2}
    assert config.long_term == {"enabled": True}  # 读得到但 MVP 不生效（SD-15）
