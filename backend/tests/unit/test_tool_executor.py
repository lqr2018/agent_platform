"""`ToolExecutor` 九步流水线（详细设计 4.2.3 / 7.3 测试要求）。

用 `ListInvocationSink` 断言第 8 步的落库产物，用 stub 工具覆盖超时 / 截断 / 取消等分支。
贯穿原则同样被锁定：**除取消外不向外抛错**、**每次调用（含被拒的）都留下
`tool_invocations` 行与 tool span**（不静默失败）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.enums import PermissionDecision, PermissionLevel, RunKind, RunStatus, SpanType
from app.core.errors import ErrorCode, RunCanceledError
from app.core.events import SseEventType
from app.runtime.agent.emitter import ListEmitter
from app.runtime.llm.base import ToolCallSpec
from app.runtime.observability.tracer import Span, Tracer
from app.runtime.tools.base import (
    TOOL_STATUS_DISABLED,
    BaseTool,
    ToolContext,
    ToolDefinition,
    ToolPermissionConfig,
    ToolResult,
)
from app.runtime.tools.executor import ListInvocationSink, ToolExecutor, ToolRunContext
from app.runtime.tools.registry import default_registry

ECHO_ID = "01J0TECHO0000000000000001"
CANCEL_ID = "01J0TCANCEL00000000000001"


class EchoTool(BaseTool):
    """回显输入；`delay` / `padding` 用于超时与输出截断分支。"""

    name = "echo"
    display_name = "回声"
    description = "回显输入文本"
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {"type": "object", "properties": {"echo": {"type": "string"}}}
    default_permission = ToolPermissionConfig(timeout_seconds=5.0)

    def __init__(self, *, delay: float = 0.0, padding: int = 0) -> None:
        self.delay = delay
        self.padding = padding

    async def run(self, ctx: ToolContext, *, text: str = "", **_: Any) -> ToolResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult(content=text + "x" * self.padding, meta={"echo": text, "marker": 1})


class CancelAwareTool(BaseTool):
    """检查 `ctx.cancellation` 的工具（4.2.3 步骤 6：只有取消会向外抛错）。"""

    name = "cancel_aware"
    description = "取消感知"
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
    default_permission = ToolPermissionConfig()

    async def run(self, ctx: ToolContext, **_: Any) -> ToolResult:
        if ctx.cancellation.is_set():
            raise RunCanceledError()
        return ToolResult(content="ok")


class SinkStub:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def write_span(self, span: Span) -> None:
        self.spans.append(span)


def _definition(tool: BaseTool, *, tool_id: str = ECHO_ID, **overrides: Any) -> ToolDefinition:
    definition = tool.definition(tool_id=tool_id)
    return replace(definition, **overrides) if overrides else definition


def _executor(
    *tools: BaseTool,
    sink: Any | None = None,
    settings: Settings | None = None,
) -> tuple[ToolExecutor, ListInvocationSink, Tracer, SinkStub]:
    registry = default_registry()
    for tool in tools:
        registry.register(tool)
    invocations = ListInvocationSink()
    span_sink = SinkStub()
    tracer = Tracer(sink=span_sink)
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")
    executor = ToolExecutor(
        registry=registry,
        settings=settings or Settings(app_env="test"),
        sink=sink or invocations,
    )
    return executor, invocations, tracer, span_sink


def _ctx(
    definitions: dict[str, ToolDefinition],
    *,
    tracer: Tracer,
    emitter: ListEmitter | None = None,
    cancel: asyncio.Event | None = None,
    calls_per_tool: dict[str, int] | None = None,
    agent_permission: ToolPermissionConfig | None = None,
    step_index: int = 2,
    call_index: int = 1,
) -> ToolRunContext:
    return ToolRunContext(
        run_id="run-1",
        definitions=definitions,
        settings=Settings(app_env="test"),
        tracer=tracer,
        emitter=emitter or ListEmitter(),
        cancellation=cancel or asyncio.Event(),
        sandbox_root=Path("./data/files"),
        agent_permission=agent_permission,
        calls_per_tool={} if calls_per_tool is None else calls_per_tool,
        step_index=step_index,
        call_index=call_index,
    )


def _call(name: str, **arguments: Any) -> ToolCallSpec:
    return ToolCallSpec(id=f"call_{name}", name=name, arguments=arguments)


class BrokenSink:
    """写库失败的 sink（4.2.3 步骤 8：写失败不得打断 Run，同 4.8.2 的 SpanSink 策略）。"""

    async def write_invocation(self, record: Any) -> None:
        raise RuntimeError("backend unavailable")


@pytest.mark.asyncio
async def test_step1_unknown_tool_is_audited_and_fed_back() -> None:
    executor, invocations, tracer, _ = _executor(EchoTool())
    emitter = ListEmitter()

    result = await executor.execute(_call("nope"), ctx=_ctx({}, tracer=tracer, emitter=emitter))

    assert result.status is RunStatus.FAILED and result.is_error
    assert result.error_code == str(ErrorCode.TOOL_NOT_FOUND)
    assert invocations.records[0].tool_id is None
    assert invocations.records[0].tool_name == "nope"
    # `tool.call.started` 只在权限与闸门通过后才发，被拒的调用只有 failed
    assert emitter.names() == [str(SseEventType.TOOL_CALL_FAILED)]


@pytest.mark.asyncio
async def test_step1_disabled_and_mcp_tools_are_rejected() -> None:
    executor, invocations, tracer, _ = _executor(EchoTool())
    disabled = _definition(EchoTool(), status=TOOL_STATUS_DISABLED)

    denied = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": disabled}, tracer=tracer))
    assert denied.error_code == str(ErrorCode.TOOL_DISABLED)
    assert invocations.records[0].permission_decision is PermissionDecision.ALLOW  # 未到步骤 4

    mcp = _definition(EchoTool(), tool_type="mcp")
    rejected = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": mcp}, tracer=tracer))
    assert rejected.error_code == str(ErrorCode.TOOL_DISABLED)
    assert rejected.meta["reason"] == "TOOL_TYPE_NOT_SUPPORTED"  # SD-16：MVP 不支持 MCP


@pytest.mark.asyncio
async def test_step3_invalid_arguments_are_fed_back_with_schema() -> None:
    executor, invocations, tracer, _ = _executor(EchoTool())
    result = await executor.execute(_call("echo"), ctx=_ctx({"echo": _definition(EchoTool())}, tracer=tracer))

    assert result.error_code == str(ErrorCode.TOOL_INVALID_ARGUMENTS)
    body = json.loads(result.content)  # 4.2.3 步骤 7：错误作为**正常结果**回填
    assert body["error"] == str(ErrorCode.TOOL_INVALID_ARGUMENTS)
    assert body["details"]["schema"]["required"] == ["text"]  # 附 schema 摘要供模型自我修正
    assert invocations.records[0].normalized_arguments == {}


@pytest.mark.asyncio
async def test_step4_denied_call_records_deny_decision() -> None:
    """`require_approval` 在 MVP = 未开启即拒绝（SD-17，不挂起、不建 approvals 行）。"""
    executor, invocations, tracer, _ = _executor(EchoTool())
    definition = _definition(EchoTool(), permission_config=ToolPermissionConfig(level=PermissionLevel.DANGEROUS))

    result = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer))

    assert result.error_code == str(ErrorCode.TOOL_PERMISSION_DENIED)
    assert result.permission_decision is PermissionDecision.DENY
    record = invocations.records[0]
    assert record.permission_decision is PermissionDecision.DENY
    assert record.error_code == str(ErrorCode.TOOL_PERMISSION_DENIED)
    assert record.approval_id is None  # SD-17 回归点


@pytest.mark.asyncio
async def test_agent_level_permission_is_merged_stricter() -> None:
    executor, _invocations, tracer, _ = _executor(EchoTool())
    ctx = _ctx(
        {"echo": _definition(EchoTool())},
        tracer=tracer,
        agent_permission=ToolPermissionConfig(require_approval=True),
    )

    result = await executor.execute(_call("echo", text="hi"), ctx=ctx)

    assert result.error_code == str(ErrorCode.TOOL_PERMISSION_DENIED)
    assert result.meta["reason"] == "APPROVAL_CHANNEL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_step5_budget_is_enforced() -> None:
    executor, invocations, tracer, _ = _executor(EchoTool())
    definition = _definition(EchoTool(), permission_config=ToolPermissionConfig(max_calls_per_run=1))
    calls = {"echo": 1}

    result = await executor.execute(
        _call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer, calls_per_tool=calls)
    )

    assert result.error_code == str(ErrorCode.TOOL_PERMISSION_DENIED)
    assert result.meta["reason"] == "MAX_CALLS_EXCEEDED"
    assert calls == {"echo": 1}  # 被拒的调用不占用配额
    assert invocations.records[0].normalized_arguments == {"text": "hi"}


@pytest.mark.asyncio
async def test_step6_success_writes_record_span_and_events() -> None:
    executor, invocations, tracer, span_sink = _executor(EchoTool())
    emitter = ListEmitter()
    calls: dict[str, int] = {}

    result = await executor.execute(
        _call("echo", text="hi"),
        ctx=_ctx({"echo": _definition(EchoTool())}, tracer=tracer, emitter=emitter, calls_per_tool=calls),
    )

    assert result.succeeded and result.content == "hi"
    assert result.permission_decision is PermissionDecision.ALLOW
    assert calls == {"echo": 1}  # 步骤 5：通过后立即占用一次配额
    assert emitter.names() == [str(SseEventType.TOOL_CALL_STARTED), str(SseEventType.TOOL_CALL_COMPLETED)]
    assert emitter.payload_for(SseEventType.TOOL_CALL_COMPLETED)["result_preview"] == "hi"

    record = invocations.records[0]
    run_trace = tracer.current_run()
    assert run_trace is not None
    assert (record.tool_id, record.tool_name) == (ECHO_ID, "echo")
    assert record.arguments == {"text": "hi"} and record.normalized_arguments == {"text": "hi"}
    assert record.status is RunStatus.SUCCEEDED and record.result == "hi"
    assert record.result_truncated is False and record.attempt == 1
    assert record.run_id == "run-1" and record.trace_id == run_trace.trace_id
    assert record.span_id and (record.step_index, record.call_index) == (2, 1)

    tool_spans = [span for span in span_sink.spans if span.span_type is SpanType.TOOL]
    assert len(tool_spans) == 1
    assert tool_spans[0].name == "tool:echo"
    assert tool_spans[0].output == "hi"
    assert tool_spans[0].attributes["echo"] == "hi" and tool_spans[0].attributes["marker"] == 1


@pytest.mark.asyncio
async def test_step6_timeout_is_fed_back_as_tool_timeout() -> None:
    slow = EchoTool(delay=0.3)
    definition = replace(_definition(slow), permission_config=ToolPermissionConfig(timeout_seconds=0.05))
    executor, invocations, tracer, _ = _executor(slow)
    emitter = ListEmitter()

    result = await executor.execute(
        _call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer, emitter=emitter)
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == str(ErrorCode.TOOL_TIMEOUT)
    assert invocations.records[0].error_code == str(ErrorCode.TOOL_TIMEOUT)
    failed = emitter.payload_for(SseEventType.TOOL_CALL_FAILED)
    assert failed["error_code"] == str(ErrorCode.TOOL_TIMEOUT) and failed["tool_call_id"] == "call_echo"


@pytest.mark.asyncio
async def test_step7_output_is_truncated_to_max_output_bytes() -> None:
    tool = EchoTool(padding=100)
    definition = replace(_definition(tool), permission_config=ToolPermissionConfig(max_output_bytes=8))
    executor, invocations, tracer, span_sink = _executor(tool)

    result = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer))

    assert result.succeeded and result.truncated is True
    assert result.content == "hi" + "x" * 6
    record = invocations.records[0]
    assert record.result_truncated is True and record.result == result.content
    tool_span = next(span for span in span_sink.spans if span.span_type is SpanType.TOOL)
    assert tool_span.attributes["truncated"] is True
    assert tool_span.attributes["original_bytes"] == 102


@pytest.mark.asyncio
async def test_step8_sink_failure_does_not_break_the_call() -> None:
    executor, _invocations, tracer, _ = _executor(EchoTool(), sink=BrokenSink())

    result = await executor.execute(
        _call("echo", text="hi"), ctx=_ctx({"echo": _definition(EchoTool())}, tracer=tracer)
    )

    assert result.succeeded and result.content == "hi"


@pytest.mark.asyncio
async def test_api_tool_renders_request_and_extracts_response_path() -> None:
    """步骤 6 的 `api` 分派：模板渲染 + `response_path` 取值（2.5 的 `http_config`）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).startswith("https://api.example.com/search?q=hi")
        assert json.loads(request.content) == {"query": "hi"}
        return httpx.Response(200, json={"data": {"answer": "ok"}})

    definition = _definition(
        EchoTool(),
        tool_type="api",
        http_config={
            "method": "POST",
            "url": "https://api.example.com/search?q={{text}}",
            "body_template": {"query": "{{text}}"},
            "response_path": "$.data.answer",
            "timeout_seconds": 1.0,
        },
        permission_config=ToolPermissionConfig(allow_network=True, allowed_hosts=["api.example.com"]),
    )
    registry = default_registry()
    invocations = ListInvocationSink()
    tracer = Tracer(sink=SinkStub())
    tracer.start_run(kind=RunKind.CHAT, name="chat:stub", run_id="run-1")
    executor = ToolExecutor(
        registry=registry,
        settings=Settings(app_env="test"),
        sink=invocations,
        client_factory=lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout),
    )

    result = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer))

    assert result.succeeded
    assert json.loads(result.content) == {"status_code": 200, "data": "ok"}
    assert invocations.records[0].result == result.content


@pytest.mark.asyncio
async def test_api_tool_is_blocked_when_network_is_not_allowed() -> None:
    """4.2.4 第 4 条：`allow_network=false`（默认）时不得出网，错误回填且不发请求。"""
    definition = _definition(
        EchoTool(),
        tool_type="api",
        http_config={"method": "GET", "url": "https://api.example.com/search?q={{text}}"},
    )
    executor, invocations, tracer, _ = _executor(EchoTool())

    result = await executor.execute(_call("echo", text="hi"), ctx=_ctx({"echo": definition}, tracer=tracer))

    assert result.error_code == str(ErrorCode.TOOL_SANDBOX_VIOLATION)
    assert result.meta["reason"] == "NETWORK_DISABLED"
    assert invocations.records[0].error_code == str(ErrorCode.TOOL_SANDBOX_VIOLATION)


@pytest.mark.asyncio
async def test_cancellation_propagates_out_of_the_executor() -> None:
    """4.2.3：除 `RunCanceledError` 外不向外抛错；取消必须原样冒泡（交给 Run 收敛）。"""
    cancel = asyncio.Event()
    cancel.set()
    tool = CancelAwareTool()
    executor, _invocations, tracer, _ = _executor(tool)
    definition = _definition(tool, tool_id=CANCEL_ID)

    with pytest.raises(RunCanceledError):
        await executor.execute(
            _call("cancel_aware"), ctx=_ctx({"cancel_aware": definition}, tracer=tracer, cancel=cancel)
        )
