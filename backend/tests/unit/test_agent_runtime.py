"""`AgentRuntime` 主循环（详细设计 4.4.2 / 4.4.3）。

用**内存版**短期记忆 + 收集型 sink，不碰数据库：Runtime 的契约（事件序列、终态、
取消、`max_steps`、超时、tool_calls 显式拒绝）在这里被固化。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import RunKind, RunStatus, SpanStatus, SpanType
from app.core.errors import FeatureNotImplementedError
from app.core.events import SseEventType
from app.runtime.agent.emitter import ListEmitter
from app.runtime.agent.runtime import AgentRuntime
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage, LLMResult, ProviderConfig, ToolCallSpec
from app.runtime.llm.fake import FakeLLMProvider
from app.runtime.llm.usage import TokenUsage
from app.runtime.observability.tracer import Span, Tracer


class MemoryStub:
    """内存版 `ShortTermMemory`（build / append / complete）。"""

    def __init__(self) -> None:
        self.history: list[ChatMessage] = []
        self.appended: list[tuple[str, ChatMessage]] = []
        self.completed: dict[str, dict[str, Any]] = {}

    async def build(
        self, conversation_id: str, *, agent: AgentSpec, before_seq: int | None = None
    ) -> list[ChatMessage]:
        return list(self.history)

    async def append(self, conversation_id: str, message: ChatMessage, *, run_id: str | None = None) -> str:
        message_id = f"msg-{len(self.appended) + 1}"
        self.appended.append((message_id, message))
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
    ) -> None:
        self.completed[message_id] = {
            "content": content,
            "finish_reason": finish_reason,
            "usage": usage,
            "model_name": model_name,
        }


class SinkStub:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def write_span(self, span: Span) -> None:
        self.spans.append(span)


class StubLLM:
    """按调用次数返回预设结果（第 N 次调用取第 N 条）。"""

    name = "stub"

    def __init__(self, results: list[LLMResult], *, delay_seconds: float = 0.0) -> None:
        self._results = results
        self._delay = delay_seconds
        self.calls = 0

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_delta: Any = None,
        on_tool_call: Any = None,
        trace_span: Span | None = None,
    ) -> LLMResult:
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if stream and on_delta is not None:
            for index in range(0, len(result.message.content), 3):
                if self._delay:
                    await asyncio.sleep(self._delay)
                await on_delta(result.message.content[index : index + 3])
        return result

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        raise NotImplementedError


def _result(content: str = "好的", *, calls: tuple[ToolCallSpec, ...] = ()) -> LLMResult:
    return LLMResult(
        message=ChatMessage.assistant(content, calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        model_name="stub-model",
        latency_ms=1,
    )


def _spec(**overrides: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "id": "agent-1",
        "name": "stub-agent",
        "model_provider_id": "provider-1",
        "model_name": "stub-model",
        "system_prompt": "你是测试助手。",
        "max_steps": 3,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return AgentSpec(**base)


def _runtime(llm: Any, memory: MemoryStub, sink: SinkStub) -> tuple[AgentRuntime, Tracer]:
    settings = Settings(app_env="test", llm_stream=True)
    tracer = Tracer(sink=sink)
    runtime = AgentRuntime(llm=llm, memory=memory, tracer=tracer, settings=settings)
    return runtime, tracer


def _fake_provider() -> FakeLLMProvider:
    return FakeLLMProvider(ProviderConfig(id="p", name="fake", kind="fake", base_url="fake://"))


@pytest.mark.asyncio
async def test_run_emits_phase1_events_and_records_spans() -> None:
    memory, sink = MemoryStub(), SinkStub()
    runtime, tracer = _runtime(_fake_provider(), memory, sink)
    emitter = ListEmitter()
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(), "你好", conversation_id="conv-1", emit=emitter)
    await tracer.end_run(tracer.current_run(), status=SpanStatus.OK)

    assert result.status is RunStatus.SUCCEEDED
    assert result.output_text.startswith("FAKE_RESPONSE")
    assert result.steps == 1 and result.usage.total_tokens == 15
    assert result.message_id == "msg-1"

    # 7.2 任务 6：Phase 1 只发 1/3/4/5/16/17 号事件
    names = emitter.names()
    assert names[0] == str(SseEventType.MESSAGE_STARTED)
    assert str(SseEventType.MESSAGE_COMPLETED) in names
    assert str(SseEventType.USAGE_UPDATED) in names
    assert str(SseEventType.AGENT_STEP_STARTED) not in names  # 阶段 2 的事件不发

    # span 是"结束时写库"，所以子 span 先于父 span 落盘（4.8.2）；顺序按 `seq` 才是 run→agent→llm
    assert [span.span_type for span in sink.spans] == [SpanType.LLM, SpanType.AGENT, SpanType.RUN]
    assert [span.seq for span in sink.spans] == [3, 2, 1]
    assert sink.spans[0].output["finish_reason"] == "stop"  # llm span
    assert memory.completed["msg-1"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_run_canceled_stops_the_loop() -> None:
    memory, sink = MemoryStub(), SinkStub()
    runtime, tracer = _runtime(_fake_provider(), memory, sink)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-2")
    cancel = asyncio.Event()
    cancel.set()

    result = await runtime.run(_spec(), "你好", conversation_id="conv-1", cancel=cancel)

    assert result.status is RunStatus.CANCELED
    assert result.run_id == "run-2"
    assert result.error_code == "RUN_CANCELED"
    assert memory.appended == []  # 还没开始就取消了


@pytest.mark.asyncio
async def test_run_timeout_is_reported_as_run_timeout() -> None:
    class SlowLLM:
        name = "slow"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResult:
            await asyncio.sleep(0.2)
            return _result()

        async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
            raise NotImplementedError

    memory, sink = MemoryStub(), SinkStub()
    runtime, tracer = _runtime(SlowLLM(), memory, sink)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-3")

    result = await runtime.run(_spec(timeout_seconds=0), "你好", conversation_id="conv-1")

    assert result.status is RunStatus.FAILED
    assert result.error_code == "RUN_TIMEOUT"


@pytest.mark.asyncio
async def test_tool_calls_are_rejected_in_phase1() -> None:
    """4.4.3 的 tool 分支属 Phase 2：本阶段显式拒绝（不静默忽略）。"""
    call = ToolCallSpec(id="call_1", name="calculator", arguments={"expression": "1+1"})
    memory, sink = MemoryStub(), SinkStub()
    runtime, tracer = _runtime(StubLLM([_result(calls=(call,))]), memory, sink)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-4")

    result = await runtime.run(_spec(), "算一下", conversation_id="conv-1")

    assert result.status is RunStatus.FAILED
    assert result.error_code == str(FeatureNotImplementedError.code)


@pytest.mark.asyncio
async def test_max_steps_is_enforced_before_first_call() -> None:
    memory, sink = MemoryStub(), SinkStub()
    runtime, tracer = _runtime(StubLLM([_result()]), memory, sink)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-5")

    result = await runtime.run(_spec(max_steps=0), "你好", conversation_id="conv-1")

    assert result.status is RunStatus.FAILED
    assert result.error_code == "MODEL_MAX_STEPS_EXCEEDED"
    assert memory.appended == []


@pytest.mark.asyncio
async def test_run_requires_active_tracer_run() -> None:
    memory, sink = MemoryStub(), SinkStub()
    runtime, _tracer = _runtime(StubLLM([_result()]), memory, sink)
    with pytest.raises(RuntimeError):
        await runtime.run(_spec(), "你好", conversation_id="conv-1")
