"""Checkpoint / resume 单测（详细设计 4.5.4 / 7.4 测试要求：从失败节点续跑、不重复执行前面的节点）。"""

from __future__ import annotations

from typing import Any

from app.core.enums import RunStatus
from app.core.errors import ErrorCode, ToolTimeoutError
from app.runtime.workflow import SimpleEngine
from app.runtime.workflow import checkpoint as checkpoint_module
from app.runtime.workflow import state as state_module
from tests.unit.workflow.stubs import StubRunner, canonical_definition, resume_engine, run_engine


def checkpoint_at(node_id: str, **business: Any) -> dict[str, Any]:
    state = state_module.initial_state({"input": "写一份调研", **business}, run={"run_id": "run-1"})
    return checkpoint_module.build_checkpoint(state, node_id)


def test_build_checkpoint_shape() -> None:
    """4.5.4：`{engine, engine_version, state, current_node_id, pending_branch}`（SD-10 的处理位）。"""
    payload = checkpoint_at("router", plan="P1")
    assert set(payload) == {"engine", "engine_version", "state", "current_node_id", "pending_branch"}
    assert payload["engine"] == "simple" and payload["engine_version"] == "1"
    assert payload["current_node_id"] == "router"
    assert payload["pending_branch"] is None
    assert payload["state"]["plan"] == "P1"


def test_parse_checkpoint_tolerates_junk() -> None:
    """字段缺失 / 类型不符 → 按"无断点"处理（不抛异常，由调用方决定降级）。"""
    empty = checkpoint_module.parse_checkpoint(None)
    assert empty.state == {} and empty.current_node_id is None and not empty.is_compatible()

    broken = checkpoint_module.parse_checkpoint({"state": "nope", "current_node_id": 5, "engine": "langgraph"})
    assert broken.state == {} and broken.current_node_id is None and not broken.is_compatible()

    valid = checkpoint_module.parse_checkpoint({"engine": "simple", "engine_version": "1", "current_node_id": "end"})
    assert valid.is_compatible() and valid.current_node_id == "end"


async def test_resume_from_failed_node_skips_previous_nodes() -> None:
    """集成口径（7.4）：失败运行 `resume` 从该节点续跑，且不重复执行前面的节点。"""
    first = await run_engine(SimpleEngine, runner=StubRunner(fail_plan={"planner": [ToolTimeoutError()]}))
    assert first.result.status is RunStatus.FAILED
    checkpoint = first.sink.checkpoints()[-1]
    assert checkpoint["current_node_id"] == "planner"

    resumed = await resume_engine(SimpleEngine, checkpoint)
    assert resumed.result.status is RunStatus.SUCCEEDED
    assert resumed.sink.sequence() == ["planner", "search", "router", "writer", "end"]
    assert resumed.runner.calls_of("start") == 0
    assert resumed.runner.calls_of("planner") == 1
    # state 从 checkpoint 带过来（4.5.4：只补跑，不丢前面的产物）
    assert resumed.result.state["input"] == "写一份调研"
    assert resumed.result.state["draft"] == "agent-reply"


async def test_resume_mid_graph_continues_from_checkpoint_node() -> None:
    checkpoint = checkpoint_at("writer", plan="P1", hits="H1")
    resumed = await resume_engine(SimpleEngine, checkpoint)

    assert resumed.result.status is RunStatus.SUCCEEDED
    assert resumed.sink.sequence() == ["writer", "end"]
    assert resumed.result.output["plan"] == "P1"
    assert resumed.result.current_node_id == "end"
    assert resumed.result.restarted is False


async def test_resume_from_end_node_is_terminal() -> None:
    resumed = await resume_engine(SimpleEngine, checkpoint_at("end"))
    assert resumed.sink.sequence() == ["end"]
    assert resumed.result.status is RunStatus.SUCCEEDED


async def test_resume_with_incompatible_engine_restarts_from_scratch() -> None:
    """SD-10：`engine` / `engine_version` 不匹配 → 降级"从头重跑"（保留已保存的 state）。"""
    checkpoint = checkpoint_at("writer", topic="T1", plan="P1")
    checkpoint["engine"] = "langgraph"
    resumed = await resume_engine(SimpleEngine, checkpoint)

    assert resumed.result.restarted is True
    assert resumed.sink.sequence() == ["start", "planner", "search", "router", "writer", "end"]
    assert resumed.result.output["topic"] == "T1"
    assert resumed.runner.calls_of("planner") == 1


async def test_resume_without_checkpoint_starts_over() -> None:
    resumed = await resume_engine(SimpleEngine, {})
    assert resumed.result.restarted is True
    assert resumed.sink.sequence()[0] == "start"


async def test_resume_keeps_iteration_counters_fresh() -> None:
    """续跑是一次新的执行：`iteration` / `attempt` 从 1 重新计数（节点级的 DoD 断言）。"""
    checkpoint = checkpoint_at("writer", plan="P1")
    resumed = await resume_engine(SimpleEngine, checkpoint)

    writer_row = resumed.sink.rows_of("writer")[0]
    assert (writer_row.iteration, writer_row.attempt) == (1, 1)


async def test_resume_error_path_still_records_node_runs() -> None:
    """续跑中再次失败 → 节点行落 `failed` + 可续跑的落点（4.5.4 的两类场景都会用到）。"""
    checkpoint = checkpoint_at("search")
    definition = canonical_definition()
    runner = StubRunner(fail_plan={"search": [ToolTimeoutError()]})
    # `search`（tool）默认 continue：错误被吞掉后继续跑，Run 仍然成功
    resumed = await resume_engine(SimpleEngine, checkpoint, definition=definition, runner=runner)
    assert resumed.result.status is RunStatus.SUCCEEDED
    assert resumed.sink.rows_of("search")[0].error_code == str(ErrorCode.TOOL_TIMEOUT)
