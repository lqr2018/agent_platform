"""Workflow 引擎契约（详细设计 4.5.1 / 4.5.3 / 4.8.1）。

4.5.1 的 Protocol 只有 `run` / `resume` 两个方法；本文件额外定义**三个注入点**，
理由与 `AgentRuntime`（4.4.1）一致 —— runtime 不 import `db` / `services`（1.2）：

- `WorkflowRunSink`：`node_runs` / `workflow_runs.state` / `checkpoint` 的落库入口
  （与 `ToolInvocationSink` 同一套风格，4.5.3）；
- `WorkflowNodeRunner`：四类"有 IO"的节点（`agent` / `tool` / `retriever` / `end` 之外的）
  实际执行者，由服务层装配（AgentRuntime / ToolExecutor 都在服务层拼装）；
- `tracer`：`workflow` / `node` span 的落点（4.8.1：`run → workflow → node → agent`）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.enums import RunStatus
from app.runtime.agent.emitter import EventEmitter
from app.runtime.observability.tracer import Tracer
from app.runtime.workflow.graph import WorkflowGraph, WorkflowNode


@dataclass(slots=True)
class NodeRunStarted:
    """`node_runs` 的开行数据（4.5.3：进入节点即落一行）。"""

    node_id: str
    node_type: str
    name: str
    seq: int
    iteration: int
    attempt: int
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NodeRunFinished:
    """`node_runs` 的收尾数据（状态 / 输出摘要 / 耗时 / 错误）。"""

    node_run_id: str
    status: RunStatus
    output: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    span_id: str | None = None
    agent_run_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class WorkflowRunSink(Protocol):
    """`workflow_runs` / `node_runs` 的落库接口（服务层实现，4.5.3）。"""

    async def node_started(self, record: NodeRunStarted) -> str:
        """插入 `node_runs` 行（`status=running`），返回 `node_run_id`。"""
        ...

    async def node_finished(self, record: NodeRunFinished) -> None:
        """更新 `node_runs` 行为终态。"""
        ...

    async def state_updated(
        self, *, state: Mapping[str, Any], current_node_id: str | None, checkpoint: Mapping[str, Any]
    ) -> None:
        """每个节点结束时更新 `workflow_runs.state` / `current_node_id` / `checkpoint`（4.5.3）。"""
        ...


class NullWorkflowRunSink:
    """不落库的 sink（单测 / conformance 模板用）。"""

    async def node_started(self, record: NodeRunStarted) -> str:
        return f"node-run-{record.seq}"

    async def node_finished(self, record: NodeRunFinished) -> None:
        return None

    async def state_updated(
        self, *, state: Mapping[str, Any], current_node_id: str | None, checkpoint: Mapping[str, Any]
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class NodeExecutionContext:
    """一次节点执行的可观测上下文（含取消信号与 tracer）。"""

    run_id: str
    node_id: str
    iteration: int
    attempt: int
    tracer: Tracer
    emitter: EventEmitter
    cancel: asyncio.Event


@dataclass(slots=True)
class NodeOutcome:
    """节点执行结果：写入 state 的键值 + 条件分支选中的下一个节点。"""

    state_updates: dict[str, Any] = field(default_factory=dict)
    next_node_id: str | None = None
    output_summary: dict[str, Any] = field(default_factory=dict)
    agent_run_id: str | None = None


class WorkflowNodeRunner(Protocol):
    """有 IO 的节点执行器（服务层注入，4.5.2）。"""

    async def run_agent_node(self, node: WorkflowNode, input_text: str, ctx: NodeExecutionContext) -> NodeOutcome: ...

    async def run_tool_node(
        self, node: WorkflowNode, arguments: Mapping[str, Any], ctx: NodeExecutionContext
    ) -> NodeOutcome: ...

    async def run_retriever_node(self, node: WorkflowNode, query: str, ctx: NodeExecutionContext) -> NodeOutcome: ...


@dataclass(slots=True)
class WorkflowResult:
    """一次 Workflow 运行的结果（引擎返回值 → 服务层落 `workflow_runs` 终态）。"""

    run_id: str
    trace_id: str
    status: RunStatus
    state: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    current_node_id: str | None = None
    steps: int = 0
    node_count: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    restarted: bool = False
    """`resume` 时因 checkpoint 与引擎不兼容而降级"从头重跑"（SD-10）。"""

    @property
    def is_success(self) -> bool:
        return self.status is RunStatus.SUCCEEDED


class WorkflowEngine(Protocol):
    """4.5.1 的引擎契约（当前只有 `SimpleEngine` 一个实现，SD-10）。"""

    engine_name: str
    engine_version: str

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
    ) -> WorkflowResult: ...

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
    ) -> WorkflowResult: ...
