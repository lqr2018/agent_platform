"""`SimpleEngine`：唯一的 Workflow 引擎（详细设计 4.5.1 / 4.5.2 / 4.5.3 / 4.5.4，SD-10）。

纯 `asyncio` 实现：自己维护"下一个节点"与循环上限，零第三方依赖、语义可见、好写单测。
**不创建 `langgraph_engine.py`**（SD-10：不为"可替换"提前写第二套实现）。

一次运行的落点（4.5.3）：

```text
run span（服务层 Tracer.start_run）
└── workflow span（本引擎）
    └── node span ×N        name=node:{node_id}（DoD 4 的可见性要求）
```

每个节点：

1. `render_inputs()` 渲染入参 → `node_runs.input`；
2. `emit(workflow.node.started)`（事件 13）；
3. `execute_node()`（`agent` / `tool` / `retriever` 走注入的 runner）；
4. 成功 → `node_runs` 置 `succeeded`、写 state、`emit(workflow.node.completed)`（事件 14）；
   失败 → 按 `on_error` 处理：`fail` 终止 Run（`WORKFLOW_NODE_FAILED`）、
   `retry(n)` 重开一行 `node_runs`（`attempt+1`）、`continue` 把错误文本写进 `output_key` 后继续
   （4.5.2：`tool` / `retriever` 默认 `continue`）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.core.enums import NodeType, RunStatus, SpanType
from app.core.errors import (
    AppError,
    ErrorCode,
    InternalError,
    RunCanceledError,
    WorkflowMaxStepsExceededError,
)
from app.core.events import SseEventType, WorkflowNodeCompletedPayload, WorkflowNodeStartedPayload
from app.core.logging import get_logger
from app.runtime.agent.emitter import EventEmitter
from app.runtime.observability.tracer import Tracer
from app.runtime.workflow import checkpoint as checkpoint_module
from app.runtime.workflow import nodes
from app.runtime.workflow import state as state_module
from app.runtime.workflow import template as template_module
from app.runtime.workflow.base import (
    NodeExecutionContext,
    NodeOutcome,
    NodeRunFinished,
    NodeRunStarted,
    WorkflowNodeRunner,
    WorkflowResult,
    WorkflowRunSink,
)
from app.runtime.workflow.graph import WorkflowGraph, WorkflowNode

logger = get_logger(__name__)

MAX_WARNINGS = 50
"""span attributes 里保留的 warning 条数上限（超出的只记日志）。"""


@dataclass(slots=True)
class _Counters:
    """一次运行的计数器（`node_runs.seq` / 步数 / 每节点进入次数）。"""

    seq: int = 0
    steps: int = 0
    node_count: int = 0
    entry_counts: dict[str, int] = field(default_factory=dict)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class SimpleEngine:
    """4.5.1 的 `SimpleEngine`（唯一引擎，SD-10）。"""

    engine_name: str = checkpoint_module.ENGINE_NAME
    engine_version: str = checkpoint_module.ENGINE_VERSION

    def __init__(self, *, max_steps: int | None = None, recursion_limit: int | None = None) -> None:
        """两个可选覆盖项仅供单测：默认取 `definition.config`（4.5.2 的默认 30 / 50）。"""
        self._max_steps = max_steps
        self._recursion_limit = recursion_limit

    # ---- 4.5.1 的两个入口 ----
    async def run(
        self,
        graph: WorkflowGraph,
        initial_state: dict[str, Any],
        *,
        run_id: str,
        emit: EventEmitter,
        cancel: asyncio.Event,
        tracer: Tracer,
        sink: WorkflowRunSink,
        runner: WorkflowNodeRunner | None = None,
    ) -> WorkflowResult:
        state = _normalize_state(initial_state)
        return await self._drive(
            graph=graph,
            state=state,
            start_node_id=graph.start_node_id,
            run_id=run_id,
            emit=emit,
            cancel=cancel,
            tracer=tracer,
            sink=sink,
            runner=runner,
            restarted=False,
        )

    async def resume(
        self,
        checkpoint: Mapping[str, Any],
        *,
        graph: WorkflowGraph,
        run_id: str,
        emit: EventEmitter,
        cancel: asyncio.Event,
        tracer: Tracer,
        sink: WorkflowRunSink,
        runner: WorkflowNodeRunner | None = None,
    ) -> WorkflowResult:
        """从 checkpoint 续跑（4.5.4 的两类场景；引擎不匹配 → 降级从头重跑，SD-10）。"""
        parsed = checkpoint_module.parse_checkpoint(checkpoint)
        start_node_id, restarted = checkpoint_module.resume_start_node(
            parsed, fallback_start_node_id=graph.start_node_id
        )
        state = _normalize_state(parsed.state)
        return await self._drive(
            graph=graph,
            state=state,
            start_node_id=start_node_id,
            run_id=run_id,
            emit=emit,
            cancel=cancel,
            tracer=tracer,
            sink=sink,
            runner=runner,
            restarted=restarted,
        )

    # ---- 主循环（4.5.2 / 4.5.3） ----
    async def _drive(
        self,
        *,
        graph: WorkflowGraph,
        state: dict[str, Any],
        start_node_id: str,
        run_id: str,
        emit: EventEmitter,
        cancel: asyncio.Event,
        tracer: Tracer,
        sink: WorkflowRunSink,
        runner: WorkflowNodeRunner | None,
        restarted: bool,
    ) -> WorkflowResult:
        started = perf_counter()
        counters = _Counters()
        warnings: list[str] = []
        status = RunStatus.SUCCEEDED
        error: AppError | None = None
        current_node_id: str | None = start_node_id
        landing_node_id: str | None = start_node_id
        """落库/展示用的"当前节点"（失败时停在失败节点，成功跑完 `end` 时停在 `end`）。"""
        max_steps = self._max_steps or int(graph.config.get("max_steps", 1))
        recursion_limit = self._recursion_limit or int(graph.config.get("recursion_limit", 1))

        async with tracer.span(
            SpanType.WORKFLOW,
            f"workflow:{graph.definition.get('name') or run_id}",
            attributes={
                "engine": self.engine_name,
                "engine_version": self.engine_version,
                "workflow_run_id": run_id,
                "start_node_id": graph.start_node_id,
                "restarted": restarted,
                "max_steps": max_steps,
                "recursion_limit": recursion_limit,
            },
            input={"state_keys": sorted(state_module.business_fields(state))},
        ) as span:
            try:
                while current_node_id is not None:
                    node = graph.node(current_node_id)
                    if cancel.is_set():
                        raise RunCanceledError()
                    counters.entry_counts[node.id] = counters.entry_counts.get(node.id, 0) + 1
                    iteration = counters.entry_counts[node.id]
                    if counters.steps + 1 > max_steps:
                        raise WorkflowMaxStepsExceededError(
                            f"Workflow exceeded max_steps={max_steps}",
                            details={"node_id": node.id, "max_steps": max_steps, "steps": counters.steps},
                        )
                    if iteration > recursion_limit:
                        raise WorkflowMaxStepsExceededError(
                            f"Workflow exceeded recursion_limit={recursion_limit} at node '{node.id}'",
                            details={"node_id": node.id, "recursion_limit": recursion_limit, "iteration": iteration},
                        )
                    counters.steps += 1
                    counters.node_count += 1
                    landing_node_id = node.id
                    # 断点：进入节点前就把 current_node_id 指向本节点（崩溃后从本节点续跑，4.5.4）
                    await _call_sink(
                        sink.state_updated(
                            state=_jsonable(state),
                            current_node_id=node.id,
                            checkpoint=checkpoint_module.build_checkpoint(_jsonable(state), node.id),
                        ),
                        action="state_updated",
                        node_id=node.id,
                    )
                    await emit.emit(
                        SseEventType.WORKFLOW_NODE_STARTED,
                        WorkflowNodeStartedPayload(node_id=node.id, node_type=str(node.type)),
                    )
                    try:
                        outcome, node_status = await self._run_with_policy(
                            node,
                            state=state,
                            counters=counters,
                            run_id=run_id,
                            emit=emit,
                            cancel=cancel,
                            tracer=tracer,
                            sink=sink,
                            runner=runner,
                            warnings=warnings,
                            iteration=iteration,
                        )
                    except (AppError, asyncio.CancelledError):
                        # 失败也要发出配对的 completed（status=failed），前端才能把节点画成红
                        await emit.emit(
                            SseEventType.WORKFLOW_NODE_COMPLETED,
                            WorkflowNodeCompletedPayload(node_id=node.id, status=str(RunStatus.FAILED)),
                        )
                        raise
                    await emit.emit(
                        SseEventType.WORKFLOW_NODE_COMPLETED,
                        WorkflowNodeCompletedPayload(node_id=node.id, status=str(node_status)),
                    )
                    if node.type is NodeType.CONDITION and outcome is not None:
                        current_node_id = outcome.next_node_id
                    else:
                        current_node_id = graph.next_node_id(node)
                    # 落点：跑完 `end` 后把 current_node_id 记成 `end`（前端展示"已跑完"，4.5.4 的断点语义）
                    landing_node_id = node.id if node.type is NodeType.END else current_node_id
                    await _call_sink(
                        sink.state_updated(
                            state=_jsonable(state),
                            current_node_id=landing_node_id,
                            checkpoint=checkpoint_module.build_checkpoint(_jsonable(state), landing_node_id),
                        ),
                        action="state_updated",
                        node_id=node.id,
                    )
            except RunCanceledError as exc:
                status, error = RunStatus.CANCELED, exc
            except AppError as exc:
                status, error = RunStatus.FAILED, exc
            except asyncio.CancelledError:
                # 交给外层（`asyncio.wait_for` 超时 / 进程收尾）收敛：这里必须**重新抛出**，
                # 否则取消信号被吞掉，`wait_for` 之外的任务取消语义会失真（服务层映射为 RUN_TIMEOUT）
                raise
            except Exception:
                logger.error("workflow.unexpected_error", run_id=run_id, exc_info=True)
                status = RunStatus.FAILED
                error = InternalError("Unexpected error while running the workflow")

            if len(warnings) > MAX_WARNINGS:
                logger.warning("workflow.warnings_truncated", run_id=run_id, total=len(warnings))
            span.attributes.update(
                {
                    "steps": counters.steps,
                    "node_count": counters.node_count,
                    "warnings": warnings[:MAX_WARNINGS],
                    "status": str(status),
                }
            )
            span.output = {
                "status": str(status),
                "current_node_id": landing_node_id,
                "output_keys": sorted(state_module.business_fields(state)),
                "error_code": str(error.code) if error is not None else None,
            }

        run_trace = tracer.current_run()
        return WorkflowResult(
            run_id=run_id,
            trace_id=run_trace.trace_id if run_trace is not None else "",
            status=status,
            state=state,
            output=state_module.business_fields(state),
            current_node_id=landing_node_id,
            steps=counters.steps,
            node_count=counters.node_count,
            latency_ms=int((perf_counter() - started) * 1000),
            error_code=str(error.code) if error is not None else None,
            error_message=error.message if error is not None else None,
            warnings=warnings,
            restarted=restarted,
        )

    # ---- 失败策略（4.5.2：fail / continue / retry(n)） ----
    async def _run_with_policy(
        self,
        node: WorkflowNode,
        *,
        state: dict[str, Any],
        counters: _Counters,
        run_id: str,
        emit: EventEmitter,
        cancel: asyncio.Event,
        tracer: Tracer,
        sink: WorkflowRunSink,
        runner: WorkflowNodeRunner | None,
        warnings: list[str],
        iteration: int,
    ) -> tuple[NodeOutcome | None, RunStatus]:
        """按 `node.on_error` 重试 / 吞掉 / 抛出；返回 `(outcome, 本次节点的状态)`。"""
        policy = node.on_error
        attempt = 1
        while True:
            try:
                outcome = await self._run_attempt(
                    node,
                    state=state,
                    counters=counters,
                    run_id=run_id,
                    emit=emit,
                    cancel=cancel,
                    tracer=tracer,
                    sink=sink,
                    runner=runner,
                    warnings=warnings,
                    iteration=iteration,
                    attempt=attempt,
                )
            except RunCanceledError:
                raise
            except AppError as exc:
                if policy.mode == "retry" and attempt < policy.max_attempts:
                    logger.info("workflow.node_retry", node_id=node.id, attempt=attempt, error_code=str(exc.code))
                    attempt += 1
                    continue
                if policy.mode == "continue" and node.type is not NodeType.CONDITION:
                    _record_continued_error(node, state=state, error=exc, warnings=warnings)
                    return None, RunStatus.FAILED
                raise
            return outcome, RunStatus.SUCCEEDED

    async def _run_attempt(
        self,
        node: WorkflowNode,
        *,
        state: dict[str, Any],
        counters: _Counters,
        run_id: str,
        emit: EventEmitter,
        cancel: asyncio.Event,
        tracer: Tracer,
        sink: WorkflowRunSink,
        runner: WorkflowNodeRunner | None,
        warnings: list[str],
        iteration: int,
        attempt: int,
    ) -> NodeOutcome:
        """一次节点尝试：`node_runs` 一行 + 一个 `node:{id}` span（4.5.3）。"""
        inputs = nodes.render_inputs(node, state=state, warnings=warnings)
        seq = counters.next_seq()
        node_run_id = await _call_sink(
            sink.node_started(
                NodeRunStarted(
                    node_id=node.id,
                    node_type=str(node.type),
                    name=node.name,
                    seq=seq,
                    iteration=iteration,
                    attempt=attempt,
                    input=_jsonable(inputs),
                )
            ),
            action="node_started",
            node_id=node.id,
        )
        started = perf_counter()
        ctx = NodeExecutionContext(
            run_id=run_id,
            node_id=node.id,
            iteration=iteration,
            attempt=attempt,
            tracer=tracer,
            emitter=emit,
            cancel=cancel,
        )
        async with tracer.span(
            SpanType.NODE,
            f"node:{node.id}",
            attributes={
                "node_type": str(node.type),
                "iteration": iteration,
                "attempt": attempt,
                "on_error": node.on_error.as_dict(),
                "output_key": node.output_key,
            },
            input=inputs,
        ) as span:
            try:
                outcome = await nodes.execute_node(
                    node, state=state, inputs=inputs, ctx=ctx, runner=runner, warnings=warnings
                )
            except asyncio.CancelledError:
                # 取消（`asyncio.wait_for` 超时 / 进程收尾）不是普通异常，但**这一行必须收尾**：
                # 否则 `node_runs` 会永久停在 `running`（前端节点表一直转圈，见 4.5.3 的状态语义）。
                # 记 `canceled` 而不是 `failed` —— 任务不是自己失败的，是外部把它掐停在执行点上。
                await _finish_attempt(
                    sink,
                    node_run_id=node_run_id,
                    node=node,
                    status=RunStatus.CANCELED,
                    output={"error": str(ErrorCode.RUN_CANCELED)},
                    latency_ms=int((perf_counter() - started) * 1000),
                    span_id=span.span_id,
                    error_code=str(ErrorCode.RUN_CANCELED),
                    error_message="Node run was canceled",
                )
                raise
            except Exception as exc:
                await _finish_attempt(
                    sink,
                    node_run_id=node_run_id,
                    node=node,
                    status=RunStatus.FAILED,
                    output={"error": str(getattr(exc, "code", type(exc).__name__))},
                    latency_ms=int((perf_counter() - started) * 1000),
                    span_id=span.span_id,
                    error_code=str(getattr(exc, "code", None) or type(exc).__name__),
                    error_message=str(getattr(exc, "message", None) or exc),
                )
                raise
            await _finish_attempt(
                sink,
                node_run_id=node_run_id,
                node=node,
                status=RunStatus.SUCCEEDED,
                output=outcome.output_summary,
                latency_ms=int((perf_counter() - started) * 1000),
                span_id=span.span_id,
                agent_run_id=outcome.agent_run_id,
            )
            return outcome


async def _call_sink(coro: Any, *, action: str, node_id: str) -> Any:
    """落库失败不影响主流程（4.5.3 与 4.8.2 同一条原则：观测/记录是副产物）。"""
    try:
        return await coro
    except Exception:  # 任何落库异常都只记日志
        logger.warning("workflow.sink_failed", action=action, node_id=node_id, exc_info=True)
        return None


async def _finish_attempt(
    sink: WorkflowRunSink,
    *,
    node_run_id: Any,
    node: WorkflowNode,
    status: RunStatus,
    output: Mapping[str, Any],
    latency_ms: int,
    span_id: str | None,
    agent_run_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """收尾一行 `node_runs`；`node_started` 失败时静默跳过（避免观测失败打断业务）。"""
    if not isinstance(node_run_id, str) or not node_run_id:
        return
    await _call_sink(
        sink.node_finished(
            NodeRunFinished(
                node_run_id=node_run_id,
                status=status,
                output=_jsonable(dict(output)),
                latency_ms=latency_ms,
                span_id=span_id,
                agent_run_id=agent_run_id,
                error_code=error_code,
                error_message=error_message,
            )
        ),
        action="node_finished",
        node_id=node.id,
    )


def _record_continued_error(node: WorkflowNode, *, state: dict[str, Any], error: AppError, warnings: list[str]) -> None:
    """`on_error=continue`：把错误文本写进 `output_key`，交给后续节点自己处理（4.5.2）。"""
    text = f"[{error.code}] {error.message}"
    state_module.set_node_output(
        state, node.id, text, output_key=node.output_key, extra={"error_code": str(error.code)}
    )
    warnings.append(f"node '{node.id}' failed but continued: {error.code}")


def _jsonable(value: Any) -> Any:
    """交给 sink 之前先转成 JSON 安全的值（4.5.3：`state` / `input` / `output` 都是 JSON 列）。"""
    return template_module.to_jsonable(value)


def _normalize_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """补齐三个命名空间（`nodes` / `run`），保证模板与节点执行有稳定的读取面。"""
    normalized = dict(state)
    if not isinstance(normalized.get(state_module.NODES_SCOPE), dict):
        normalized[state_module.NODES_SCOPE] = {}
    if not isinstance(normalized.get(state_module.RUN_SCOPE), dict):
        normalized[state_module.RUN_SCOPE] = {}
    return normalized
