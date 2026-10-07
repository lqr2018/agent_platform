"""Workflow 单测的替身与图构造便捷函数（详细设计 9.2：测试替身复用规则）。

- `RecordingSpanSink` / `make_tracer()`：`Tracer` 的内存 sink（不落库，便于断言 span 树）；
- `RecordingSink`：`WorkflowRunSink` 的内存实现（`node_runs` 序列 / state 快照 / checkpoint）；
- `StubRunner`：`WorkflowNodeRunner` 的脚本化实现（可控失败、可统计调用次数）；
- `canonical_definition()`：`start → agent → tool → condition → (writer) → end` 的标准图，
  conformance 模板、resume、分支测试共用同一张图。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import RunKind, RunStatus, SpanStatus
from app.core.errors import AppError
from app.runtime.agent.emitter import ListEmitter
from app.runtime.observability.tracer import Span, Tracer
from app.runtime.workflow import WorkflowGraph, WorkflowNode, parse_definition
from app.runtime.workflow import state as state_module
from app.runtime.workflow.base import NodeOutcome, NodeRunFinished, NodeRunStarted


class RecordingSpanSink:
    """`Tracer` 的内存 sink。"""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    async def write_span(self, span: Span) -> None:
        self.spans.append(span)

    def names(self) -> list[str]:
        return [span.name for span in self.spans]

    def named(self, name: str) -> Span:
        for span in self.spans:
            if span.name == name:
                return span
        raise AssertionError(f"span '{name}' not found; got {self.names()}")


def make_tracer() -> tuple[Tracer, RecordingSpanSink]:
    """返回 `(tracer, sink)`（`store_io=True`，便于断言 input/output）。"""
    sink = RecordingSpanSink()
    return Tracer(sink=sink, store_io=True), sink


@dataclass(slots=True)
class NodeRunRow:
    """合并 `node_started` / `node_finished` 之后的内存行（对应 `node_runs` 一行）。"""

    node_run_id: str
    node_id: str
    node_type: str
    name: str
    seq: int
    iteration: int
    attempt: int
    status: RunStatus
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    agent_run_id: str | None = None


class RecordingSink:
    """`WorkflowRunSink` 的内存实现。"""

    def __init__(self) -> None:
        self.started: list[NodeRunStarted] = []
        self.finished: list[NodeRunFinished] = []
        self.node_runs: list[NodeRunRow] = []
        self.state_updates: list[dict[str, Any]] = []

    async def node_started(self, record: NodeRunStarted) -> str:
        self.started.append(record)
        node_run_id = f"nr-{record.seq}"
        self.node_runs.append(
            NodeRunRow(
                node_run_id=node_run_id,
                node_id=record.node_id,
                node_type=record.node_type,
                name=record.name,
                seq=record.seq,
                iteration=record.iteration,
                attempt=record.attempt,
                status=RunStatus.RUNNING,
                input=dict(record.input),
            )
        )
        return node_run_id

    async def node_finished(self, record: NodeRunFinished) -> None:
        self.finished.append(record)
        for row in self.node_runs:
            if row.node_run_id == record.node_run_id:
                row.status = record.status
                row.output = dict(record.output)
                row.error_code = record.error_code
                row.agent_run_id = record.agent_run_id
                return
        raise AssertionError(f"unknown node_run_id {record.node_run_id}")

    async def state_updated(
        self, *, state: dict[str, Any], current_node_id: str | None, checkpoint: dict[str, Any]
    ) -> None:
        self.state_updates.append(
            {"state": dict(state), "current_node_id": current_node_id, "checkpoint": dict(checkpoint)}
        )

    # ---- 断言辅助 ----
    def sequence(self) -> list[str]:
        return [row.node_id for row in self.node_runs]

    def rows_of(self, node_id: str) -> list[NodeRunRow]:
        return [row for row in self.node_runs if row.node_id == node_id]

    def attempts_of(self, node_id: str) -> list[int]:
        return [row.attempt for row in self.rows_of(node_id)]

    def checkpoints(self) -> list[dict[str, Any]]:
        return [item["checkpoint"] for item in self.state_updates]

    def last_state(self) -> dict[str, Any]:
        return dict(self.state_updates[-1]["state"]) if self.state_updates else {}


@dataclass
class StubRunner:
    """`WorkflowNodeRunner` 的脚本化实现。

    `fail_plan[node_id]` 是**按 attempt 顺序**的脚本：`None` 表示该次成功，
    非 `None` 表示抛该异常（长度不足时视为成功）。
    """

    agent_reply: str = "agent-reply"
    tool_result: str = "tool-result"
    retriever_result: str = "chunk-1"
    fail_plan: dict[str, list[AppError | None]] = field(default_factory=dict)
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def _maybe_fail(self, node: WorkflowNode, attempt: int) -> None:
        plan = self.fail_plan.get(node.id, [])
        error = plan[attempt - 1] if attempt <= len(plan) else None
        if error is not None:
            raise error

    def calls_of(self, node_id: str) -> int:
        return sum(1 for _, name, _ in self.calls if name == node_id)

    async def run_agent_node(self, node: WorkflowNode, input_text: str, ctx: Any) -> NodeOutcome:
        self.calls.append(("agent", node.id, ctx.attempt))
        self._maybe_fail(node, ctx.attempt)
        return NodeOutcome(
            state_updates={"output": self.agent_reply},
            output_summary={"agent_id": node.str_field("agent_id"), "input": input_text},
            agent_run_id="run-shared",
        )

    async def run_tool_node(self, node: WorkflowNode, arguments: Any, ctx: Any) -> NodeOutcome:
        self.calls.append(("tool", node.id, ctx.attempt))
        self._maybe_fail(node, ctx.attempt)
        return NodeOutcome(
            state_updates={"output": self.tool_result},
            output_summary={"tool_name": node.str_field("tool_name"), "arguments": dict(arguments)},
        )

    async def run_retriever_node(self, node: WorkflowNode, query: str, ctx: Any) -> NodeOutcome:
        self.calls.append(("retriever", node.id, ctx.attempt))
        self._maybe_fail(node, ctx.attempt)
        return NodeOutcome(
            state_updates={"output": self.retriever_result},
            output_summary={"kb_id": node.str_field("kb_id"), "query": query},
        )


def canonical_definition(*, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """标准图：`start → planner(agent) → search(tool) → router(condition) → writer(agent) → end`。"""
    return {
        "name": "canonical",
        "nodes": [
            {"id": "start", "type": "start", "name": "开始", "next": "planner"},
            {
                "id": "planner",
                "type": "agent",
                "name": "规划",
                "agent_id": "agt_planner",
                "input_template": "{{state.input}}",
                "output_key": "plan",
                "next": "search",
            },
            {
                "id": "search",
                "type": "tool",
                "name": "检索",
                "tool_name": "web_search",
                "arguments_template": {"query": "{{state.plan}}"},
                "output_key": "hits",
                "next": "router",
            },
            {
                "id": "router",
                "type": "condition",
                "name": "是否命中",
                "branches": [{"when": "len(state.hits) > 0", "next": "writer"}],
                "default_next": "end",
            },
            {
                "id": "writer",
                "type": "agent",
                "name": "撰写",
                "agent_id": "agt_writer",
                "input_template": "{{state.hits}}",
                "output_key": "draft",
                "next": "end",
            },
            {"id": "end", "type": "end", "name": "结束"},
        ],
        "config": config or {"max_steps": 10, "recursion_limit": 5, "timeout_seconds": 60},
    }


def canonical_graph(*, config: dict[str, Any] | None = None) -> WorkflowGraph:
    return parse_definition(canonical_definition(config=config))


RUN_ID = "01J8Z0000000000000000000R1"
ENGINE_RUN_ID = RUN_ID
"""单测里固定的 Run id（`node_runs` / span 的断言都用它）。"""


class Harness:
    """一次运行的观测集合（结果 + 落库替身 + 事件 + span + runner）。"""

    def __init__(
        self,
        *,
        result: Any,
        sink: RecordingSink,
        emitter: ListEmitter,
        span_sink: RecordingSpanSink,
        runner: StubRunner,
    ) -> None:
        self.result = result
        self.sink = sink
        self.emitter = emitter
        self.span_sink = span_sink
        self.runner = runner

    def event_names(self) -> list[str]:
        return self.emitter.names()

    def node_events(self) -> list[str]:
        return [name for name in self.event_names() if name.startswith("workflow.node.")]


async def run_engine(
    engine: Any,
    *,
    definition: dict[str, Any] | None = None,
    runner: StubRunner | None = None,
    sink: RecordingSink | None = None,
    initial: dict[str, Any] | None = None,
    cancel: asyncio.Event | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    use_runner: bool = True,
) -> Harness:
    """跑一遍图（含 `Tracer.start_run`，与 4.8.1 的 `run → workflow → node` 层级一致）。

    `use_runner=False` 用于验证"服务层忘了注入 runner"时的失败路径（4.5.2）。
    """
    graph = parse_definition(definition or canonical_definition())
    stub_sink = sink or RecordingSink()
    stub_runner = runner if runner is not None else StubRunner()
    tracer, span_sink = make_tracer()
    emitter = ListEmitter()
    run_trace = tracer.start_run(kind=RunKind.WORKFLOW, name="workflow-test", run_id=RUN_ID)
    state = state_module.initial_state(
        initial or {"input": "写一份调研"},
        run={"run_id": run_trace.run_id, "workflow_id": "wf-canonical"},
    )
    instance = engine(**(engine_kwargs or {}))
    result = await instance.run(
        graph,
        state,
        run_id=run_trace.run_id,
        emit=emitter,
        cancel=cancel or asyncio.Event(),
        tracer=tracer,
        sink=stub_sink,
        runner=stub_runner if use_runner else None,
    )
    await tracer.end_run(run_trace, status=SpanStatus.OK)
    return Harness(result=result, sink=stub_sink, emitter=emitter, span_sink=span_sink, runner=stub_runner)


async def resume_engine(
    engine: Any,
    checkpoint: dict[str, Any],
    *,
    definition: dict[str, Any] | None = None,
    runner: StubRunner | None = None,
    sink: RecordingSink | None = None,
) -> Harness:
    """从 `checkpoint` 续跑（`workflow_runs.checkpoint` 的还原路径，4.5.4）。"""
    graph = parse_definition(definition or canonical_definition())
    stub_sink = sink or RecordingSink()
    stub_runner = runner if runner is not None else StubRunner()
    tracer, span_sink = make_tracer()
    emitter = ListEmitter()
    run_trace = tracer.start_run(kind=RunKind.WORKFLOW, name="workflow-test", run_id=RUN_ID)
    result = await engine().resume(
        checkpoint,
        graph=graph,
        run_id=run_trace.run_id,
        emit=emitter,
        cancel=asyncio.Event(),
        tracer=tracer,
        sink=stub_sink,
        runner=stub_runner,
    )
    await tracer.end_run(run_trace, status=SpanStatus.OK)
    return Harness(result=result, sink=stub_sink, emitter=emitter, span_sink=span_sink, runner=stub_runner)
