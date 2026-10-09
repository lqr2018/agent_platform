"""Workflow 引擎 conformance 模板（详细设计 7.4 / 4.5.1 / 4.5.3）。

**用法**：新增引擎实现时，把它追加到 `ENGINE_FACTORIES` 参数表 —— 同一张标准图必须产出
相同的 `node_runs` 序列、state 与事件序列。SD-10：当前只有 `SimpleEngine` 一个实现，
**不创建 `langgraph_engine.py`**，但模板保持"参数化引擎"的形态。

标准图（`stubs.canonical_definition`）：
`start → planner(agent) → search(tool) → router(condition) → writer(agent) → end`。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.enums import NodeType, RunKind, RunStatus, SpanType
from app.core.events import SseEventType
from app.runtime.agent.emitter import ListEmitter
from app.runtime.workflow import NodeOutcome, SimpleEngine, WorkflowNode
from app.runtime.workflow import state as state_module
from tests.unit.workflow.stubs import (
    RUN_ID,
    RecordingSink,
    StubRunner,
    canonical_graph,
    make_tracer,
    run_engine,
)

ENGINE_FACTORIES: list[Any] = [pytest.param(SimpleEngine, id="simple")]
"""引擎参数表（SD-10：加第二个实现时在此追加，conformance 全套自动覆盖）。"""


@pytest.mark.parametrize("engine", ENGINE_FACTORIES)
async def test_conformance_canonical_graph(engine: Any) -> None:
    """conformance：`node_runs` 序列 / state / 事件四件套一次钉住（7.4 测试要求）。"""
    harness = await run_engine(engine)
    sink = harness.sink

    assert sink.sequence() == ["start", "planner", "search", "router", "writer", "end"]
    assert [row.seq for row in sink.node_runs] == [1, 2, 3, 4, 5, 6]
    assert {row.status for row in sink.node_runs} == {RunStatus.SUCCEEDED}
    assert {row.iteration for row in sink.node_runs} == {1}
    assert {row.attempt for row in sink.node_runs} == {1}
    assert [row.node_type for row in sink.node_runs] == [
        str(NodeType.START),
        str(NodeType.AGENT),
        str(NodeType.TOOL),
        str(NodeType.CONDITION),
        str(NodeType.AGENT),
        str(NodeType.END),
    ]

    result = harness.result
    assert result.status is RunStatus.SUCCEEDED
    assert result.steps == 6 and result.node_count == 6
    assert result.current_node_id == "end"
    assert result.error_code is None
    assert result.output["plan"] == "agent-reply"
    assert result.output["hits"] == "tool-result"
    assert result.output["draft"] == "agent-reply"
    assert result.output["input"] == "写一份调研"

    # state 命名空间：`output_key` 与 `nodes.<id>.output` 同时可读（4.5.2）
    final_state = state_module.snapshot(result.state)
    assert state_module.node_output(final_state, "planner") == "agent-reply"
    assert final_state["nodes"]["search"]["output"] == "tool-result"

    # 事件 13/14：每个节点一对，顺序与 node_runs 一致
    assert harness.node_events() == [
        event
        for _ in sink.sequence()
        for event in (str(SseEventType.WORKFLOW_NODE_STARTED), str(SseEventType.WORKFLOW_NODE_COMPLETED))
    ]
    assert harness.emitter.payload_for(SseEventType.WORKFLOW_NODE_STARTED) == {
        "node_id": "start",
        "node_type": str(NodeType.START),
    }
    assert harness.emitter.payload_for(SseEventType.WORKFLOW_NODE_COMPLETED) == {
        "node_id": "start",
        "status": str(RunStatus.SUCCEEDED),
    }


@pytest.mark.parametrize("engine", ENGINE_FACTORIES)
async def test_conformance_spans_and_checkpoints(engine: Any) -> None:
    """span 层级（4.8.1：`run → workflow → node:{id}`）+ 节点前后的 checkpoint（4.5.4）。"""
    harness = await run_engine(engine)
    root_span = harness.span_sink.named("workflow-test")
    assert root_span.span_type is SpanType.RUN

    workflow_span = harness.span_sink.named("workflow:canonical")
    assert workflow_span.span_type is SpanType.WORKFLOW
    assert workflow_span.parent_span_id == root_span.span_id
    assert workflow_span.attributes["engine"] == "simple"
    assert workflow_span.attributes["engine_version"] == "1"

    node_span = harness.span_sink.named("node:planner")
    assert node_span.span_type is SpanType.NODE
    assert node_span.parent_span_id == workflow_span.span_id
    assert node_span.attributes["node_type"] == str(NodeType.AGENT)
    assert node_span.input == {"text": "写一份调研"}
    assert harness.span_sink.named("node:router").attributes["on_error"] == {"mode": "fail", "retries": 0}
    # 4.8.1：一条 Run 一个 trace 树（run → workflow → node 全在同一条 trace 上）
    assert {span.trace_id for span in harness.span_sink.spans} == {root_span.trace_id}

    # 每个节点两条 state_updated：进入前（current=本节点）与结束后（current=下一个节点）
    checkpoints = harness.sink.checkpoints()
    assert len(checkpoints) == len(harness.sink.node_runs) * 2
    assert all(item["engine"] == "simple" and item["engine_version"] == "1" for item in checkpoints)
    assert [item["current_node_id"] for item in checkpoints[:4]] == ["start", "planner", "planner", "search"]
    assert checkpoints[-1]["current_node_id"] == "end"
    assert checkpoints[-1]["state"]["draft"] == "agent-reply"


async def test_cancel_during_node_execution_finishes_node_run_row() -> None:
    """W3：取消落在节点执行中时，该 `node_runs` 行必须收尾（否则永久停在 `running`）。

    `CancelledError` 是 `BaseException`，不会进 `except Exception` —— 修复前 planner 那一行
    永远停在 `running`：`asyncio.wait_for` 超时 / 进程收尾都会踩到这个窗口，
    前端节点表于是永远转圈（4.5.3 的状态语义被破坏）。
    """
    sink = RecordingSink()
    tracer, _ = make_tracer()
    emitter = ListEmitter()
    entered = asyncio.Event()

    class BlockingRunner(StubRunner):
        """在 agent 节点上阻塞，直到测试自己取消这个任务。"""

        async def run_agent_node(self, node: WorkflowNode, input_text: str, ctx: Any) -> NodeOutcome:
            entered.set()
            await asyncio.Event().wait()
            return await super().run_agent_node(node, input_text, ctx)

    tracer.start_run(kind=RunKind.WORKFLOW, name="workflow-test", run_id=RUN_ID)
    task = asyncio.create_task(
        SimpleEngine().run(
            canonical_graph(),
            state_module.initial_state({"input": "写一份调研"}, run={"run_id": RUN_ID, "workflow_id": "wf"}),
            run_id=RUN_ID,
            emit=emitter,
            cancel=asyncio.Event(),
            tracer=tracer,
            sink=sink,
            runner=BlockingRunner(),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    planner_rows = sink.rows_of("planner")
    assert len(planner_rows) == 1
    assert planner_rows[0].status is RunStatus.CANCELED
    assert planner_rows[0].error_code == "RUN_CANCELED"
    assert [row.node_id for row in sink.node_runs if row.status is RunStatus.RUNNING] == []
