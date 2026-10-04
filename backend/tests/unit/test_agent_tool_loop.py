"""`AgentRuntime` 的工具分支（详细设计 4.4.3 / 7.3 测试要求）。

用内存版记忆 + 真实 `ToolExecutor`（只跑 `calculator` / 被拒的 `file_write`）+ `ListInvocationSink`，
把 messages 序列、`tool_invocations` 行、事件序列与"重复失败终止"固化下来，不碰数据库。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import PermissionDecision, RunKind, RunStatus, SpanStatus, SpanType
from app.core.events import SseEventType
from app.runtime.agent.emitter import ListEmitter
from app.runtime.agent.runtime import AgentRuntime, ToolKit
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage, LLMResult, ToolCallSpec
from app.runtime.llm.usage import TokenUsage
from app.runtime.observability.tracer import Span, Tracer
from app.runtime.tools.base import ToolDefinition
from app.runtime.tools.executor import ListInvocationSink, ToolExecutor
from app.runtime.tools.registry import builtin_definitions, builtin_tool_id, default_registry


class MemoryStub:
    """内存版 `ShortTermMemory`（记录 append / complete 的入参）。"""

    def __init__(self) -> None:
        self.appended: list[tuple[str, ChatMessage]] = []
        self.completed: dict[str, dict[str, Any]] = {}

    async def build(
        self, conversation_id: str, *, agent: AgentSpec, before_seq: int | None = None
    ) -> list[ChatMessage]:
        return []

    async def append(self, conversation_id: str, message: ChatMessage, *, run_id: str | None = None) -> str:
        message_id = f"msg-{len(self.appended) + 1}"
        self.appended.append((message_id, message))
        return message_id

    async def complete(self, message_id: str, **fields: Any) -> None:
        self.completed[message_id] = fields


class SinkStub:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def write_span(self, span: Span) -> None:
        self.spans.append(span)


class DefinitionsHolder:
    """可变的定义源：模拟"运行期工具被禁用 / 移除"（4.2.2：每一步重新计算）。"""

    def __init__(self, definitions: Sequence[ToolDefinition]) -> None:
        self.definitions = list(definitions)
        self.calls = 0

    async def __call__(self) -> list[ToolDefinition]:
        self.calls += 1
        return list(self.definitions)


class StubLLM:
    """按调用次数返回预设结果，并记录每次下发的 `tools`（4.2.2 可见性断言点）。"""

    name = "stub"

    def __init__(self, results: list[LLMResult]) -> None:
        self._results = results
        self.tools_seen: list[list[dict[str, Any]] | None] = []

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
        self.tools_seen.append(tools)
        return self._results[min(len(self.tools_seen) - 1, len(self._results) - 1)]

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        raise NotImplementedError


def _result(content: str = "", *, calls: tuple[ToolCallSpec, ...] = ()) -> LLMResult:
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
        "tool_ids": (builtin_tool_id("calculator"),),
        "max_steps": 6,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return AgentSpec(**base)


def _runtime(
    llm: StubLLM, memory: MemoryStub, sink: SinkStub, definitions: DefinitionsHolder
) -> tuple[AgentRuntime, ToolKit, ListInvocationSink, Tracer]:
    settings = Settings(app_env="test", llm_stream=False)
    tracer = Tracer(sink=sink)
    invocations = ListInvocationSink()
    registry = default_registry()
    kit = ToolKit(
        registry=registry,
        executor=ToolExecutor(registry=registry, settings=settings, sink=invocations),
        load_definitions=definitions,
    )
    runtime = AgentRuntime(llm=llm, memory=memory, tracer=tracer, settings=settings, toolkit=kit)
    return runtime, kit, invocations, tracer


class DisablingHolder(DefinitionsHolder):
    """第一次加载后清空定义：模拟"运行期把工具禁用"（4.2.2 要求下一步立即生效）。"""

    async def __call__(self) -> list[ToolDefinition]:
        definitions = await super().__call__()
        if self.calls == 1:
            self.definitions = []
        return definitions


@pytest.mark.asyncio
async def test_tool_loop_runs_calculator_and_persists_tool_message() -> None:
    call = ToolCallSpec(id="call_1", name="calculator", arguments={"expression": "2+2"})
    llm = StubLLM([_result(calls=(call,)), _result("结果是 4")])
    memory, sink = MemoryStub(), SinkStub()
    holder = DefinitionsHolder(builtin_definitions())
    runtime, _kit, invocations, tracer = _runtime(llm, memory, sink, holder)
    emitter = ListEmitter()
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(), "算一下 2+2", conversation_id="conv-1", emit=emitter)
    await tracer.end_run(tracer.current_run(), status=SpanStatus.OK)

    assert result.status is RunStatus.SUCCEEDED
    assert result.output_text == "结果是 4"
    assert (result.steps, result.tool_call_count) == (2, 1)
    assert holder.calls == 2  # 4.2.2：每一步都重新计算可见工具

    # 7.3 的集成断言：messages 序列 = user → assistant(tool_calls) → tool → assistant(stop)
    assert [str(message.role) for _id, message in memory.appended] == ["assistant", "tool", "assistant"]
    tool_message = memory.appended[1][1]
    assert (tool_message.tool_call_id, tool_message.name) == ("call_1", "calculator")
    assert tool_message.content == "2+2 = 4"
    # assistant 行必须留下 tool_calls，否则回放上下文断链（4.4.3 / memory.base 的签名细化）
    assert memory.completed["msg-1"]["tool_calls"] == (call,)
    assert memory.completed["msg-1"]["finish_reason"] == "tool_calls"

    record = invocations.records[0]
    assert record.tool_id == builtin_tool_id("calculator") and record.status is RunStatus.SUCCEEDED
    assert record.arguments == {"expression": "2+2"}
    assert (record.step_index, record.call_index) == (1, 1)

    names = emitter.names()
    assert names.count(str(SseEventType.AGENT_STEP_STARTED)) == 2
    assert names.count(str(SseEventType.AGENT_STEP_COMPLETED)) == 2
    assert str(SseEventType.TOOL_CALL_STARTED) in names and str(SseEventType.TOOL_CALL_COMPLETED) in names
    steps = [payload for name, payload in emitter.events if name == str(SseEventType.AGENT_STEP_COMPLETED)]
    assert [item["has_tool_calls"] for item in steps] == [True, False]

    # 4.2.2：两次调用都下发了 calculator 的 function schema（无工具时才传 None）
    assert llm.tools_seen[0] is not None and llm.tools_seen[1] is not None
    assert llm.tools_seen[0][0]["function"]["name"] == "calculator"

    # 每个工具调用在 Trace 中都有独立 tool span（7.3 DoD）
    tool_spans = [span for span in sink.spans if span.span_type is SpanType.TOOL]
    assert len(tool_spans) == 1 and tool_spans[0].name == "tool:calculator"
    assert tool_spans[0].input == {"arguments": {"expression": "2+2"}}


@pytest.mark.asyncio
async def test_denied_tool_is_fed_back_and_run_continues() -> None:
    """7.3 回归点：`file_write` 未开启 → 被拒并回填，模型可继续给出最终回答（错误回填闭环）。"""
    call = ToolCallSpec(id="call_1", name="file_write", arguments={"path": "outputs/a.txt", "content": "hi"})
    llm = StubLLM([_result(calls=(call,)), _result("我改用只读方式")])
    memory, sink = MemoryStub(), SinkStub()
    holder = DefinitionsHolder(builtin_definitions())
    runtime, _kit, invocations, tracer = _runtime(llm, memory, sink, holder)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(
        _spec(tool_ids=(builtin_tool_id("calculator"), builtin_tool_id("file_write"))),
        "写入文件",
        conversation_id="conv-1",
    )

    assert result.status is RunStatus.SUCCEEDED  # 被拒不中断 Run（4.2.3 错误处理原则）
    assert result.output_text == "我改用只读方式"
    record = invocations.records[0]
    assert record.status is RunStatus.FAILED
    assert record.error_code == "TOOL_PERMISSION_DENIED"
    assert record.permission_decision is PermissionDecision.DENY
    assert record.approval_id is None  # SD-17：不建 approvals 行
    assert "TOOL_PERMISSION_DENIED" in memory.appended[1][1].content
    assert memory.appended[1][1].role.value == "tool"


@pytest.mark.asyncio
async def test_hallucinated_tool_is_fed_back_as_not_found() -> None:
    call = ToolCallSpec(id="call_1", name="kb_search", arguments={"query": "x"})
    llm = StubLLM([_result(calls=(call,)), _result("没有可用工具，直接回答")])
    memory, sink = MemoryStub(), SinkStub()
    runtime, _kit, invocations, tracer = _runtime(llm, memory, sink, DefinitionsHolder(builtin_definitions()))
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(), "检索一下", conversation_id="conv-1")

    assert result.status is RunStatus.SUCCEEDED
    assert invocations.records[0].error_code == "TOOL_NOT_FOUND"
    assert invocations.records[0].tool_id is None
    assert "TOOL_NOT_FOUND" in memory.appended[1][1].content


@pytest.mark.asyncio
async def test_repeated_tool_failure_fails_the_run() -> None:
    """4.4.3：同一工具连续失败 3 次 → Run 失败 / `TOOL_REPEATED_FAILURE`（不静默重试）。"""
    call = ToolCallSpec(id="call_1", name="calculator", arguments={"expression": "1/0"})
    llm = StubLLM([_result(calls=(call,))])  # 每步都返回同一个必然失败的调用
    memory, sink = MemoryStub(), SinkStub()
    runtime, _kit, invocations, tracer = _runtime(llm, memory, sink, DefinitionsHolder(builtin_definitions()))
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(max_steps=8), "算一下", conversation_id="conv-1")

    assert result.status is RunStatus.FAILED
    assert result.error_code == "TOOL_REPEATED_FAILURE"
    assert (result.steps, result.tool_call_count) == (3, 3)
    assert len(invocations.records) == 3
    assert {str(record.error_code) for record in invocations.records} == {"TOOL_EXECUTION_FAILED"}


@pytest.mark.asyncio
async def test_tools_are_recomputed_each_step() -> None:
    call = ToolCallSpec(id="call_1", name="calculator", arguments={"expression": "2+2"})
    llm = StubLLM([_result(calls=(call,)), _result("done")])
    holder = DisablingHolder(builtin_definitions())
    runtime, _kit, _invocations, tracer = _runtime(llm, MemoryStub(), SinkStub(), holder)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(), "算一下", conversation_id="conv-1")

    assert result.status is RunStatus.SUCCEEDED
    assert llm.tools_seen[0] is not None
    assert llm.tools_seen[1] is None  # 第一步后工具被禁用 → 第二步不再下发（4.2.2）


@pytest.mark.asyncio
async def test_agent_without_tool_ids_sends_no_tools() -> None:
    llm = StubLLM([_result("你好")])
    holder = DefinitionsHolder(builtin_definitions())
    runtime, _kit, invocations, tracer = _runtime(llm, MemoryStub(), SinkStub(), holder)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(tool_ids=()), "你好", conversation_id="conv-1")

    assert result.status is RunStatus.SUCCEEDED
    assert llm.tools_seen == [None]  # 4.1.2 第 4 条：无工具 Agent 不下发 tools
    assert holder.calls == 0
    assert invocations.records == []


@pytest.mark.asyncio
async def test_disabled_definition_is_filtered_out_before_the_call() -> None:
    disabled = [replace(item, status="disabled") for item in builtin_definitions()]
    llm = StubLLM([_result("你好")])
    holder = DefinitionsHolder(disabled)
    runtime, _kit, _invocations, tracer = _runtime(llm, MemoryStub(), SinkStub(), holder)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")

    result = await runtime.run(_spec(), "你好", conversation_id="conv-1")

    assert result.status is RunStatus.SUCCEEDED
    assert holder.calls == 1
    assert llm.tools_seen == [None]
