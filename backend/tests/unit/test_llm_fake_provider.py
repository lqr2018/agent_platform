"""`FakeLLMProvider` 的行为契约（详细设计 4.1.3：Phase 1–7 的测试底座，必须稳定）。"""

from __future__ import annotations

import pytest

from app.core.errors import ModelAuthFailedError, ModelTimeoutError
from app.runtime.llm.base import ChatMessage, ProviderConfig
from app.runtime.llm.fake import FakeLLMProvider

CALCULATOR_TOOL = {
    "type": "function",
    "function": {"name": "calculator", "description": "", "parameters": {"type": "object", "properties": {}}},
}


def _provider() -> FakeLLMProvider:
    return FakeLLMProvider(ProviderConfig(id="p1", name="fake", kind="fake", base_url="fake://"))


@pytest.mark.asyncio
async def test_default_response_and_usage() -> None:
    result = await _provider().chat([ChatMessage.user("你好")], model="fake-model")
    assert result.finish_reason == "stop"
    assert result.message.content == "FAKE_RESPONSE: 你好"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (10, 5)
    assert result.cost_usd == 0 and result.cost_estimated is False


@pytest.mark.asyncio
async def test_streaming_calls_on_delta_with_chunks() -> None:
    deltas: list[str] = []

    async def on_delta(piece: str) -> None:
        deltas.append(piece)

    result = await _provider().chat([ChatMessage.user("abc")], model="m", stream=True, on_delta=on_delta)
    assert "".join(deltas) == result.message.content
    assert len(deltas) > 1  # 按 chunk_size 切分


@pytest.mark.asyncio
async def test_simulate_error_from_params_and_from_text() -> None:
    with pytest.raises(ModelTimeoutError):
        await _provider().chat([ChatMessage.user("hi")], model="m", params={"simulate_error": "timeout"})
    with pytest.raises(ModelAuthFailedError):
        await _provider().chat([ChatMessage.user("请 simulate_error=auth")], model="m")


@pytest.mark.asyncio
async def test_tool_call_then_second_round_summarizes() -> None:
    """4.1.3：首轮产生 tool_calls，第二轮（已有 tool 消息）直接给结论。"""
    provider = _provider()
    first = await provider.chat([ChatMessage.user("帮我计算 (128*4+16)/8")], model="m", tools=[CALCULATOR_TOOL])
    assert first.finish_reason == "tool_calls"
    assert first.message.tool_calls[0].name == "calculator"
    assert first.message.tool_calls[0].arguments["expression"] == "(128*4+16)/8"

    second = await provider.chat(
        [
            ChatMessage.user("帮我计算 (128*4+16)/8"),
            ChatMessage.assistant("", first.message.tool_calls),
            ChatMessage.tool("72", tool_call_id="call_1", name="calculator"),
        ],
        model="m",
        tools=[CALCULATOR_TOOL],
    )
    assert second.finish_reason == "stop"
    assert second.message.content == "结果：72"


@pytest.mark.asyncio
async def test_embed_is_not_implemented_in_phase1() -> None:
    from app.core.errors import FeatureNotImplementedError

    with pytest.raises(FeatureNotImplementedError):
        await _provider().embed(["a"], model="m")
