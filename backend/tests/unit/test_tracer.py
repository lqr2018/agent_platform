"""Tracer 骨架单测（7.1 任务 5；1.5.2 / 4.8.1 / 4.8.2）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.enums import RunKind, SpanStatus, SpanType
from app.core.errors import NotFoundError
from app.runtime.observability import context as trace_context
from app.runtime.observability.tracer import Span, Tracer


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def write_span(self, span: Span) -> None:
        self.spans.append(span)


class BrokenSink:
    async def write_span(self, span: Span) -> None:
        raise RuntimeError("disk on fire")


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def tracer(sink: RecordingSink) -> Tracer:
    return Tracer(sink=sink, store_io=True, max_payload_bytes=64)


async def test_start_run_binds_trace_context(tracer: Tracer) -> None:
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1", run_id="01J" + "0" * 23)
    try:
        assert run.kind is RunKind.CHAT
        assert run.root_span.span_type is SpanType.RUN
        assert run.root_span.seq == 1
        assert run.root_span.parent_span_id is None
        assert trace_context.get_trace_id() == run.trace_id
        assert trace_context.get_span_id() == run.root_span.span_id
    finally:
        await tracer.end_run(run)


async def test_end_run_clears_context_and_emits_root_span(tracer: Tracer, sink: RecordingSink) -> None:
    run = tracer.start_run(kind=RunKind.WORKFLOW, name="workflow:demo")
    await tracer.end_run(run)
    assert trace_context.get_trace_id() is None
    assert [span.span_type for span in sink.spans] == [SpanType.RUN]
    assert sink.spans[0].status is SpanStatus.OK
    assert sink.spans[0].latency_ms is not None


async def test_nested_spans_parent_and_seq(tracer: Tracer, sink: RecordingSink) -> None:
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    async with tracer.span(SpanType.AGENT, "agent:demo") as agent_span:
        async with tracer.span(SpanType.LLM, "llm:qwen-plus", attributes={"model": "qwen-plus"}) as llm_span:
            assert llm_span.parent_span_id == agent_span.span_id
            assert trace_context.get_span_id() == llm_span.span_id
        # 退出内层后恢复父 span（上下文传播，1.5.2）
        assert trace_context.get_span_id() == agent_span.span_id
    await tracer.end_run(run)

    assert [span.name for span in sink.spans] == ["llm:qwen-plus", "agent:demo", "chat:agent-1"]
    # seq 按"开始顺序"分配（根 span 占 1），sink 按"结束顺序"收到
    assert [span.seq for span in sink.spans] == [3, 2, 1]
    assert sink.spans[0].status is SpanStatus.OK


async def test_app_error_is_recorded_and_reraised(tracer: Tracer, sink: RecordingSink) -> None:
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    with pytest.raises(NotFoundError):
        async with tracer.span(SpanType.TOOL, "tool:calculator"):
            raise NotFoundError("missing tool")
    await tracer.end_run(run)
    failed = sink.spans[0]
    assert failed.status is SpanStatus.ERROR
    assert failed.error_code == "NOT_FOUND"
    assert failed.error_message == "missing tool"


async def test_unexpected_error_uses_exception_class_name(tracer: Tracer, sink: RecordingSink) -> None:
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    with pytest.raises(ZeroDivisionError):
        async with tracer.span(SpanType.LLM, "llm:broken"):
            raise ZeroDivisionError("boom")
    await tracer.end_run(run)
    assert sink.spans[0].error_code == "ZeroDivisionError"


async def test_payload_truncation_and_redaction(tracer: Tracer, sink: RecordingSink) -> None:
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    async with tracer.span(
        SpanType.LLM,
        "llm:qwen-plus",
        input={"messages": ["x" * 500]},
        attributes={"api_key": "sk-abcdefghijklmn"},
    ) as span:
        span.output = {"text": "y" * 500}
    await tracer.end_run(run)

    llm_span = sink.spans[0]
    assert isinstance(llm_span.input, dict)
    assert llm_span.input["truncated"] is True
    assert llm_span.input["original_bytes"] > 64
    assert isinstance(llm_span.output, dict)
    assert llm_span.output["truncated"] is True
    assert llm_span.attributes["api_key"] == "***"


async def test_store_io_disabled_keeps_payloads_empty(sink: RecordingSink) -> None:
    tracer = Tracer(sink=sink, store_io=False)
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    async with tracer.span(SpanType.LLM, "llm:qwen-plus", input={"messages": []}) as span:
        span.output = {"text": "hi"}
    await tracer.end_run(run)
    assert sink.spans[0].input is None
    assert sink.spans[0].output is None


async def test_sink_failure_does_not_break_business_flow() -> None:
    tracer = Tracer(sink=BrokenSink())
    run = tracer.start_run(kind=RunKind.CHAT, name="chat:agent-1")
    async with tracer.span(SpanType.TOOL, "tool:calculator"):
        pass
    await tracer.end_run(run)  # 不抛出（4.8.2：可观测副产物不影响主流程）


def test_span_requires_active_run() -> None:
    tracer = Tracer(sink=RecordingSink())

    async def _run() -> None:
        async with tracer.span(SpanType.LLM, "llm:x"):
            pass  # pragma: no cover - 进不来

    with pytest.raises(RuntimeError, match="requires an active run"):
        asyncio.run(_run())


def test_from_settings_uses_trace_settings() -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None, trace_store_io=False, trace_max_payload_bytes=128)
    tracer = Tracer.from_settings(settings, sink=RecordingSink())
    assert tracer.store_io is False
    assert tracer.max_payload_bytes == 128


def test_span_log_fields_exclude_payloads() -> None:
    span = Span(
        span_id="a" * 16,
        trace_id="b" * 32,
        run_id="c" * 26,
        parent_span_id=None,
        span_type=SpanType.LLM,
        name="llm:qwen-plus",
        seq=2,
        input={"messages": ["secret"]},
    )
    fields: dict[str, Any] = span.log_fields()
    assert "input" not in fields
    assert fields["span_type"] == "llm"
