"""Agent Runtime：平台的核心循环（详细设计 4.4 / 7.3）。

Phase 2 范围（7.3 任务 6）：**含工具分支**的主循环

```text
while step_index < agent.max_steps:
    step_index += 1; emit(agent.step.started)
    tools = visible_tools()                              # 4.2.2：每一步重新计算
    message_id = memory.append(assistant 占位行)          # 稳定 id，供 message.started/delta
    emit(message.started)
    result = llm.chat(messages, tools=tools, stream=..., on_delta=...)   # 落 llm span（4.8.1）
    累加 usage; memory.complete(..., tool_calls=...); emit(message.completed)
    if result.message.tool_calls:
        for call in result.message.tool_calls:            # 顺序执行（SD-2：不实现并行）
            inv = tool_executor.execute(call)             # 被拒/失败都作为结果回填，不挂起（SD-17）
            memory.append(tool(inv))                      # role=tool 消息落库（7.3 任务 6）
        emit(agent.step.completed, has_tool_calls=true); continue
    emit(agent.step.completed, has_tool_calls=false); succeeded
```

`emit` 与 `cancel` 都是注入的（4.4.1），工具执行器同样由服务层装配后注入（`ToolKit`）——
因此 runtime 既不依赖 Web 框架，也不 import `db` / `services`（1.2）：`tools` 行由服务层
在 `ToolKit.load_definitions` 里按步重查，`tool_invocations` 由注入的 `ToolInvocationSink` 落库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import perf_counter

from app.core.config import Settings
from app.core.enums import MessageRole, RunStatus, SpanType
from app.core.errors import (
    AppError,
    FeatureNotImplementedError,
    InternalError,
    RunCanceledError,
)
from app.core.events import (
    AgentStepCompletedPayload,
    AgentStepStartedPayload,
    MessageCompletedPayload,
    MessageDeltaPayload,
    MessageStartedPayload,
    SseEventType,
    UsageUpdatedPayload,
)
from app.core.logging import get_logger
from app.runtime.agent.context import assemble_context
from app.runtime.agent.emitter import EventEmitter, NullEmitter
from app.runtime.agent.state import AgentSpec, AgentState, RunResult
from app.runtime.agent.stop import StopReason, error_for, evaluate, repeated_tool_failure
from app.runtime.llm.base import ChatMessage, LLMProvider, LLMResult, ToolCallSpec
from app.runtime.memory.base import ShortTermMemory
from app.runtime.observability.tracer import Span, Tracer, utcnow
from app.runtime.tools.base import (
    ToolDefinition,
    ToolInvocationResult,
    ToolPermissionConfig,
)
from app.runtime.tools.executor import ToolExecutor, ToolRunContext
from app.runtime.tools.registry import ToolRegistry

logger = get_logger(__name__)

MAX_SPAN_OUTPUT_CHARS = 2_000
"""span 里留存的文本上限（4.8.2 还会按 `TRACE_MAX_PAYLOAD_BYTES` 再截一次）。"""

DefinitionsLoader = Callable[[], Awaitable[Sequence[ToolDefinition]]]
"""按步加载工具定义（服务层实现：查 `tools` 表 → `ToolDefinition` 快照）。"""


@dataclass(frozen=True, slots=True)
class ToolKit:
    """一次 Run 的工具装配（4.2.2 可见性 + 4.2.3 执行）。

    - `registry`：内置工具实现（4.2.3 步骤 6 的 `builtin` 分派点）；
    - `executor`：九步流水线（含注入的 `ToolInvocationSink`，步骤 8 落库）；
    - `load_definitions`：**每一步**调用一次，运行期禁用工具能立即生效（4.2.2）；
    - `agent_permission`：Agent 级权限，与工具级取更严格者（4.2.3 步骤 2）。
    """

    registry: ToolRegistry
    executor: ToolExecutor
    load_definitions: DefinitionsLoader
    agent_permission: ToolPermissionConfig | None = None


class AgentRuntime:
    """4.4.1 的 `AgentRuntime`。"""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        memory: ShortTermMemory,
        tracer: Tracer,
        settings: Settings,
        toolkit: ToolKit | None = None,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._tracer = tracer
        self._settings = settings
        self._toolkit = toolkit

    async def run(
        self,
        agent: AgentSpec,
        user_input: str,
        *,
        conversation_id: str | None = None,
        emit: EventEmitter | None = None,
        cancel: asyncio.Event | None = None,
        before_seq: int | None = None,
        retrieved_context: Sequence[str] | None = None,
    ) -> RunResult:
        """跑一次 Agent；调用方需先 `tracer.start_run(...)`（4.8.2）。"""
        emitter = emit or NullEmitter()
        cancel_event = cancel or asyncio.Event()
        run_trace = self._tracer.current_run()
        if run_trace is None:
            raise RuntimeError("AgentRuntime.run() requires an active Tracer run; call Tracer.start_run() first")

        state = AgentState(
            run_id=run_trace.run_id,
            trace_id=run_trace.trace_id,
            agent=agent,
            conversation_id=conversation_id,
            started_at=utcnow(),
        )
        state.reset_context(
            await assemble_context(
                agent=agent,
                memory=self._memory,
                conversation_id=conversation_id,
                user_input=user_input,
                before_seq=before_seq,
                retrieved_context=retrieved_context,
            )
        )

        started = perf_counter()
        error: AppError | None = None
        status = RunStatus.SUCCEEDED
        async with self._tracer.span(
            SpanType.AGENT,
            f"agent:{agent.name}",
            attributes={"agent_id": agent.id, "prompt_version": agent.prompt_version, "max_steps": agent.max_steps},
            input={"user_input": user_input, "messages": len(state.messages)},
        ) as agent_span:
            try:
                await asyncio.wait_for(self._loop(state, emitter, cancel_event), timeout=float(agent.timeout_seconds))
            except TimeoutError:
                error = error_for(StopReason.TIMEOUT)
                status = RunStatus.FAILED
            except AppError as exc:
                error = exc
                status = RunStatus.CANCELED if isinstance(exc, RunCanceledError) else RunStatus.FAILED
            except Exception as exc:
                logger.error("agent.run_unexpected_error", error_type=type(exc).__name__, exc_info=True)
                error = InternalError("Unexpected error while running the agent")
                status = RunStatus.FAILED
            self._close_agent_span(agent_span, state)

        return RunResult(
            run_id=state.run_id,
            trace_id=state.trace_id,
            status=status,
            output_text=_last_assistant_text(state),
            message_id=state.message_id,
            steps=state.step_index,
            tool_call_count=state.tool_call_count,
            usage=state.usage,
            cost_usd=state.cost_usd,
            latency_ms=int((perf_counter() - started) * 1000),
            finish_reason=state.finish_reason,
            error_code=str(error.code) if error is not None else None,
            error_message=error.message if error is not None else None,
        )

    @staticmethod
    def _close_agent_span(span: Span, state: AgentState) -> None:
        """`agent` span 收尾：输出摘要 + 用量（4.8.1 / 4.8.3）。"""
        span.output = {"text": _truncate(_last_assistant_text(state))}
        span.attributes.update({"steps": state.step_index, **state.usage.as_dict(), "cost_usd": float(state.cost_usd)})
        # 4.8.3：用量向上累加到父 span（run）与 traces
        span.prompt_tokens = state.usage.prompt_tokens
        span.completion_tokens = state.usage.completion_tokens
        span.cost_usd = state.cost_usd

    # ---- 主循环（4.4.3） ----
    async def _loop(self, state: AgentState, emit: EventEmitter, cancel: asyncio.Event) -> None:
        agent = state.agent
        while True:
            reason = evaluate(
                canceled=cancel.is_set(),
                step_index=state.step_index,
                max_steps=agent.max_steps,
                started_at=state.started_at,
                now=utcnow(),
                timeout_seconds=agent.timeout_seconds,
            )
            if reason is not None:
                raise error_for(reason)

            state.step_index += 1
            await self._refresh_tools(state)  # 4.2.2：每一步重新计算可见工具
            await emit.emit(
                SseEventType.AGENT_STEP_STARTED,
                AgentStepStartedPayload(step_index=state.step_index, max_steps=agent.max_steps),
            )
            message_id = await self._memory.append(
                _require_conversation(state), ChatMessage.assistant(""), run_id=state.run_id
            )
            state.message_id = message_id
            await emit.emit(
                SseEventType.MESSAGE_STARTED,
                MessageStartedPayload(message_id=message_id, role=str(MessageRole.ASSISTANT), model=agent.model_name),
            )
            result = await self._call_llm(state, emit, cancel, message_id)
            if cancel.is_set():
                raise RunCanceledError()

            await self._memory.complete(
                message_id,
                content=result.message.content,
                finish_reason=result.finish_reason,
                usage=result.usage,
                latency_ms=result.latency_ms,
                model_name=result.model_name,
                tool_calls=result.message.tool_calls,
            )
            await emit.emit(
                SseEventType.MESSAGE_COMPLETED,
                MessageCompletedPayload(
                    message_id=message_id,
                    finish_reason=result.finish_reason,
                    content=result.message.content,
                ),
            )
            state.messages.append(result.message)
            state.finish_reason = result.finish_reason

            has_tool_calls = bool(result.message.tool_calls)
            if has_tool_calls:
                await self._run_tool_calls(state, emit, cancel, result.message.tool_calls)
            await emit.emit(
                SseEventType.AGENT_STEP_COMPLETED,
                AgentStepCompletedPayload(step_index=state.step_index, has_tool_calls=has_tool_calls),
            )
            if not has_tool_calls:
                return

    # ---- 工具分支（4.4.3 / 4.2.2 / 4.2.3） ----
    async def _refresh_tools(self, state: AgentState) -> None:
        """按 `agent.tool_ids` 重新计算本步骤可见的工具（4.2.2：顺序即下发顺序）。"""
        if self._toolkit is None or not state.agent.tool_ids:
            state.tool_definitions = []
            return
        definitions = await self._toolkit.load_definitions()
        state.tool_definitions = self._toolkit.registry.visible_definitions(
            definitions, agent_tool_ids=state.agent.tool_ids
        )

    async def _run_tool_calls(
        self,
        state: AgentState,
        emit: EventEmitter,
        cancel: asyncio.Event,
        calls: Sequence[ToolCallSpec],
    ) -> None:
        """顺序执行一个 step 内的全部 tool call（SD-2），逐个落 `role=tool` 消息（7.3 任务 6）。"""
        if self._toolkit is None:
            raise FeatureNotImplementedError(
                "Tool calling requires an AgentRuntime built with a ToolKit",
                details={"tool_calls": [call.name for call in calls]},
            )
        conversation_id = _require_conversation(state)
        definitions = {item.name: item for item in state.tool_definitions}
        for index, call in enumerate(calls, start=1):
            if cancel.is_set():
                raise RunCanceledError()
            ctx = ToolRunContext(
                run_id=state.run_id,
                definitions=definitions,
                settings=self._settings,
                tracer=self._tracer,
                emitter=emit,
                cancellation=cancel,
                sandbox_root=self._settings.sandbox_path,
                agent_permission=self._toolkit.agent_permission,
                calls_per_tool=state.calls_per_tool,
                step_index=state.step_index,
                call_index=index,
            )
            invocation = await self._toolkit.executor.execute(call, ctx=ctx)
            state.tool_call_count += 1
            message = invocation.to_message()
            await self._memory.append(conversation_id, message, run_id=state.run_id)
            state.messages.append(message)
            self._note_tool_outcome(state, invocation)

    @staticmethod
    def _note_tool_outcome(state: AgentState, invocation: ToolInvocationResult) -> None:
        """4.4.3：同一工具**连续**失败 3 次 → Run 失败（`TOOL_REPEATED_FAILURE`）。"""
        if invocation.succeeded:
            state.recent_tool_failures.pop(invocation.tool_name, None)
            return
        state.recent_tool_failures[invocation.tool_name] = state.recent_tool_failures.get(invocation.tool_name, 0) + 1
        if repeated_tool_failure(state.recent_tool_failures, invocation.tool_name):
            raise error_for(StopReason.REPEATED_TOOL_FAILURE)

    async def _call_llm(
        self,
        state: AgentState,
        emit: EventEmitter,
        cancel: asyncio.Event,
        message_id: str,
    ) -> LLMResult:
        """一次 LLM 调用（含 `llm` span、增量回调、usage 累加与 `usage.updated` 事件）。"""
        agent = state.agent

        async def on_delta(piece: str) -> None:
            if cancel.is_set():
                raise RunCanceledError()
            await emit.emit(SseEventType.MESSAGE_DELTA, MessageDeltaPayload(message_id=message_id, delta=piece))

        # 4.2.2：只下发本步骤可见的工具；无可见工具时按 4.1.2 第 4 条传 None（纯对话）
        schemas = ToolRegistry.function_schemas(state.tool_definitions)

        async with self._tracer.span(
            SpanType.LLM,
            f"llm:{agent.model_name}",
            attributes={
                "model": agent.model_name,
                "stream": self._settings.llm_stream,
                "temperature": agent.model_params.get("temperature"),
                "provider_id": agent.model_provider_id,
                "tool_count": len(schemas),
            },
            input={
                "messages": _messages_digest(state.messages),
                "tools": [item.name for item in state.tool_definitions],
            },
        ) as span:
            result = await self._llm.chat(
                state.messages,
                model=agent.model_name,
                tools=schemas or None,
                params=agent.model_params,
                stream=self._settings.llm_stream,
                on_delta=on_delta,
                trace_span=span,
            )
            span.output = {"content": _truncate(result.message.content), "finish_reason": result.finish_reason}
            span.attributes.update({"usage_estimated": result.usage_estimated, "cost_estimated": result.cost_estimated})
            span.prompt_tokens = result.usage.prompt_tokens
            span.completion_tokens = result.usage.completion_tokens
            span.cost_usd = result.cost_usd

        state.usage = state.usage + result.usage
        state.cost_usd = state.cost_usd + result.cost_usd
        await emit.emit(
            SseEventType.USAGE_UPDATED,
            UsageUpdatedPayload(cost_usd=float(state.cost_usd), **result.usage.as_dict()),
        )
        return result


def _require_conversation(state: AgentState) -> str:
    if not state.conversation_id:
        raise InternalError("AgentRuntime requires a conversation_id to persist messages")
    return state.conversation_id


def _last_assistant_text(state: AgentState) -> str:
    for message in reversed(state.messages):
        if message.role == MessageRole.ASSISTANT and message.content:
            return message.content
    return ""


def _truncate(text: str, limit: int = MAX_SPAN_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _messages_digest(messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
    """Trace 里的上下文摘要（完整内容仍在 `messages` 表，4.8.2）。"""
    return [{"role": str(message.role), "chars": len(message.content)} for message in messages]
