"""节点执行语义与失败策略单测（详细设计 4.5.2 / 4.5.3）。

覆盖：`condition` 分支选择、`on_error` 的 `fail` / `continue` / `retry(n)`、
`max_steps` / `recursion_limit` 双重上限、取消、未注入 runner 的错误、入参渲染与 `output_key` 映射。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from app.core.enums import RunStatus
from app.core.errors import ErrorCode, KnowledgeBaseNotFoundError, ToolTimeoutError
from app.runtime.workflow import SimpleEngine
from tests.unit.workflow.stubs import StubRunner, canonical_definition, run_engine


def definition_with(**node_patches: Any) -> dict[str, Any]:
    """标准图 + 按节点 id 打补丁（`definition_with(search={"on_error": "retry(2)"})`）。"""
    definition = copy.deepcopy(canonical_definition())
    for node_id, patch in node_patches.items():
        target = next(node for node in definition["nodes"] if node["id"] == node_id)
        target.update(patch)
    return definition


async def test_condition_default_branch_skips_writer() -> None:
    """4.5.2：`branches` 都不命中 → 走 `default_next`，未经过的节点不产生 `node_runs`。"""
    runner = StubRunner(tool_result="")
    harness = await run_engine(SimpleEngine, runner=runner)

    assert harness.result.status is RunStatus.SUCCEEDED
    assert harness.sink.sequence() == ["start", "planner", "search", "router", "end"]
    assert runner.calls_of("writer") == 0
    assert harness.sink.rows_of("router")[0].output["matched"] == "default_next"


async def test_tool_failure_is_swallowed_and_recorded() -> None:
    """4.5.2：`tool` 默认 `continue` —— 错误文本写进 `output_key`，Run 继续。"""
    runner = StubRunner(fail_plan={"search": [ToolTimeoutError()]})
    harness = await run_engine(SimpleEngine, runner=runner)

    assert harness.result.status is RunStatus.SUCCEEDED
    search_rows = harness.sink.rows_of("search")
    assert [row.status for row in search_rows] == [RunStatus.FAILED]
    assert search_rows[0].error_code == str(ErrorCode.TOOL_TIMEOUT)
    assert harness.result.state["hits"] == "[TOOL_TIMEOUT] Tool execution timed out"
    assert harness.result.state["nodes"]["search"]["error_code"] == str(ErrorCode.TOOL_TIMEOUT)
    assert any("failed but continued" in item for item in harness.result.warnings)


async def test_agent_failure_fails_the_run() -> None:
    """4.5.2：`agent` 默认 `fail` —— Run 失败，`current_node_id` 停在失败节点（可续跑）。"""
    runner = StubRunner(fail_plan={"planner": [ToolTimeoutError()]})
    harness = await run_engine(SimpleEngine, runner=runner)

    assert harness.result.status is RunStatus.FAILED
    assert harness.result.error_code == str(ErrorCode.TOOL_TIMEOUT)
    assert harness.result.current_node_id == "planner"
    assert harness.sink.sequence() == ["start", "planner"]
    assert harness.sink.checkpoints()[-1]["current_node_id"] == "planner"
    # 失败节点也发一对事件（`completed` 的 status=failed），前端才能把节点画成红
    assert harness.node_events()[-2:] == ["workflow.node.started", "workflow.node.completed"]
    assert harness.emitter.events[-1][1] == {"node_id": "planner", "status": str(RunStatus.FAILED)}


async def test_retry_policy_retries_then_succeeds() -> None:
    """`on_error=retry(2)`：同一节点重开 `node_runs` 行，`attempt` 递增。"""
    runner = StubRunner(fail_plan={"search": [ToolTimeoutError(), None]})
    harness = await run_engine(SimpleEngine, definition=definition_with(search={"on_error": "retry(2)"}), runner=runner)

    assert harness.result.status is RunStatus.SUCCEEDED
    rows = harness.sink.rows_of("search")
    assert [row.attempt for row in rows] == [1, 2]
    assert [row.status for row in rows] == [RunStatus.FAILED, RunStatus.SUCCEEDED]
    assert [row.seq for row in rows] == [3, 4]
    assert harness.result.state["hits"] == "tool-result"


async def test_retry_exhausted_fails_the_run() -> None:
    runner = StubRunner(fail_plan={"search": [ToolTimeoutError(), ToolTimeoutError()]})
    harness = await run_engine(SimpleEngine, definition=definition_with(search={"on_error": "retry(1)"}), runner=runner)

    assert harness.result.status is RunStatus.FAILED
    assert harness.sink.attempts_of("search") == [1, 2]


async def test_retriever_node_default_continue() -> None:
    """`retriever` 默认 `continue`（4.5.2）：节点失败但 Run 继续，错误文本写进 `output_key`。

    用**真实错误类型**（Phase 5 起 `kb_service` 抛的是 `KB_NOT_FOUND`）；真实服务层的端到端见
    `tests/integration/test_knowledge_api.py::test_workflow_retriever_missing_knowledge_base_continues`。
    """
    definition = canonical_definition()
    definition["nodes"] = [
        *copy.deepcopy(definition["nodes"]),
        {"id": "reader", "type": "retriever", "kb_id": "kb-1", "output_key": "chunks", "next": "end"},
    ]
    next(node for node in definition["nodes"] if node["id"] == "writer")["next"] = "reader"
    runner = StubRunner(fail_plan={"reader": [KnowledgeBaseNotFoundError("Knowledge base 'kb-1' was not found")]})
    harness = await run_engine(SimpleEngine, definition=definition, runner=runner)

    assert harness.result.status is RunStatus.SUCCEEDED
    assert harness.sink.rows_of("reader")[0].status is RunStatus.FAILED
    assert "KB_NOT_FOUND" in str(harness.result.state["chunks"])


async def test_max_steps_limit() -> None:
    """4.5.2：`config.max_steps` 是硬上限（超限 → `WORKFLOW_MAX_STEPS_EXCEEDED`）。"""
    harness = await run_engine(SimpleEngine, definition=canonical_definition(config={"max_steps": 3}))

    assert harness.result.status is RunStatus.FAILED
    assert harness.result.error_code == str(ErrorCode.WORKFLOW_MAX_STEPS_EXCEEDED)
    assert harness.sink.sequence() == ["start", "planner", "search"]


async def test_recursion_limit_is_per_node() -> None:
    """`recursion_limit` 限制**同一节点**被进入的次数（与 `max_steps` 双重限制）。"""
    definition = {
        "name": "loop",
        "nodes": [
            {"id": "start", "type": "start", "next": "tick"},
            {"id": "tick", "type": "agent", "agent_id": "agt_1", "output_key": "tick", "next": "router"},
            {
                "id": "router",
                "type": "condition",
                "branches": [{"when": "True", "next": "tick"}],
                "default_next": "end",
            },
            {"id": "end", "type": "end"},
        ],
        "config": {"max_steps": 50, "recursion_limit": 2, "timeout_seconds": 60},
    }
    harness = await run_engine(SimpleEngine, definition=definition)

    assert harness.result.error_code == str(ErrorCode.WORKFLOW_MAX_STEPS_EXCEEDED)
    assert harness.sink.sequence() == ["start", "tick", "router", "tick", "router"]
    assert [row.iteration for row in harness.sink.rows_of("tick")] == [1, 2]


async def test_cancel_before_first_node() -> None:
    """取消：进入任一节点前检查 `cancel`（4.4.3 的同类语义）。"""
    cancel = asyncio.Event()
    cancel.set()
    harness = await run_engine(SimpleEngine, cancel=cancel)

    assert harness.result.status is RunStatus.CANCELED
    assert harness.result.error_code == str(ErrorCode.RUN_CANCELED)
    assert harness.sink.node_runs == []


async def test_missing_runner_is_a_node_failure() -> None:
    """未注入 runner（服务层未装配）→ 节点失败，而不是"静默跳过"。"""
    harness = await run_engine(SimpleEngine, use_runner=False)

    assert harness.result.status is RunStatus.FAILED
    assert harness.result.error_code == str(ErrorCode.WORKFLOW_NODE_FAILED)
    assert "none was injected" in str(harness.result.error_message)
    # `start` 不需要 runner；`planner` 需要 → 失败
    assert harness.sink.sequence() == ["start", "planner"]
    assert harness.sink.rows_of("planner")[0].output["error"] == str(ErrorCode.WORKFLOW_NODE_FAILED)
