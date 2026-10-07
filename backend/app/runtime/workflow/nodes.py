"""节点执行语义（详细设计 4.5.2）。

六类节点（**无 `human`**，SD-17）：

| 节点 | 输入 | 输出（写入 state） | 失败处理 |
|---|---|---|---|
| `start` | `workflow_runs.input` | 原样进入 state（引擎初始化时已写入） | — |
| `agent` | `input_template`（默认 `{{state.input}}`） | `output_key` ← Agent 最终回答 | 默认 `fail` |
| `tool` | `arguments_template` | `output_key` ← 工具结果 | 默认 `continue` |
| `retriever` | `query_template` + `kb_id` | `output_key` ← `RetrievedChunk[]` | `continue`（Phase 3 恒失败，见下） |
| `condition` | `branches[].when` | 无（只决定跳转） | 求值异常 → 节点失败 |
| `end` | — | state 写入 `workflow_runs.output` | — |

`agent` / `tool` / `retriever` 的**实际 IO 由服务层注入的 `WorkflowNodeRunner` 完成**
（runtime 不 import db / services，1.2）；本模块只负责模板求值、`output_key` 映射与分支选择。

执行分两步（引擎需要在**执行前**拿到渲染好的入参，用它填 `node_runs.input`）：

1. `render_inputs()`：纯函数，渲染模板（`TemplateSyntaxError` → `WORKFLOW_NODE_FAILED`）；
2. `execute_node()`：调用注入的 runner / 选择分支，并把产物映射到 `output_key`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.enums import NodeType
from app.core.errors import WorkflowNodeFailedError
from app.runtime.workflow import state as state_module
from app.runtime.workflow import template as template_module
from app.runtime.workflow.base import NodeExecutionContext, NodeOutcome, WorkflowNodeRunner
from app.runtime.workflow.graph import DEFAULT_AGENT_INPUT_TEMPLATE, WorkflowNode

OUTCOME_VALUE_KEY = "output"
"""`NodeOutcome.state_updates` 里"节点产物本体"的保留键：由本模块映射到 `output_key`。"""

PREVIEW_MAX_CHARS = 500
"""`node_runs.output.preview` 的截断长度（4.5.3："输出摘要"，大文本留在 span / 业务表）。"""


def _preview(value: Any) -> Any:
    """输出摘要（`node_runs.output.preview`）。"""
    if isinstance(value, str):
        return value if len(value) <= PREVIEW_MAX_CHARS else f"{value[:PREVIEW_MAX_CHARS]}…"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return {"type": "list", "length": len(value), "preview": str(value)[:PREVIEW_MAX_CHARS]}
    if isinstance(value, Mapping):
        return {"type": "object", "keys": sorted(str(key) for key in value)[:20]}
    return str(value)[:PREVIEW_MAX_CHARS]


def _as_text(value: Any) -> str:
    """模板渲染结果 → 文本（结构化入参转 JSON，便于 `agent` 节点直接吃 dict）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return template_module.json_dumps(value)
    return str(value)


def _require_runner(node: WorkflowNode, runner: WorkflowNodeRunner | None) -> WorkflowNodeRunner:
    if runner is None:
        raise WorkflowNodeFailedError(
            f"Node '{node.id}' ({node.type}) requires a node runner, but none was injected",
            details={"node_id": node.id, "node_type": str(node.type)},
        )
    return runner


def _apply_output(state: dict[str, Any], node: WorkflowNode, outcome: NodeOutcome) -> dict[str, Any]:
    """把 `NodeOutcome` 落到 state，并生成 `node_runs.output` 的摘要（4.5.2 / 4.5.3）。"""
    summary = dict(outcome.output_summary)
    if OUTCOME_VALUE_KEY in outcome.state_updates:
        value = outcome.state_updates.pop(OUTCOME_VALUE_KEY)
        state_module.set_node_output(state, node.id, value, output_key=node.output_key)
        summary["output_key"] = node.output_key
        summary["preview"] = _preview(value)
    state.update(outcome.state_updates)
    return summary


def render_inputs(node: WorkflowNode, *, state: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """渲染节点入参（`node_runs.input` 的内容；4.5.2 的 `*_template`）。"""
    if node.type in (NodeType.START, NodeType.END):
        return {"state_keys": sorted(state_module.business_fields(state))}
    context = template_module.TemplateContext.from_state(state)
    try:
        if node.type is NodeType.AGENT:
            return {
                "text": _as_text(
                    template_module.render_text(
                        node.str_field("input_template") or DEFAULT_AGENT_INPUT_TEMPLATE,
                        context,
                        warnings=warnings,
                    )
                )
            }
        if node.type is NodeType.TOOL:
            rendered = template_module.render_value(node.get("arguments_template") or {}, context, warnings=warnings)
            if not isinstance(rendered, Mapping):
                raise WorkflowNodeFailedError(
                    f"arguments_template of node '{node.id}' must render to an object",
                    details={"node_id": node.id, "rendered_type": type(rendered).__name__},
                )
            return {"arguments": dict(rendered)}
        if node.type is NodeType.RETRIEVER:
            return {
                "query": _as_text(
                    template_module.render_text(
                        node.str_field("query_template") or DEFAULT_AGENT_INPUT_TEMPLATE,
                        context,
                        warnings=warnings,
                    )
                ),
                "kb_id": node.str_field("kb_id"),
            }
        return {"branches": [branch.when for branch in node.branches], "default_next": node.default_next}
    except template_module.TemplateSyntaxError as exc:
        raise WorkflowNodeFailedError(
            f"Node '{node.id}' has an invalid template: {exc}",
            details={"node_id": node.id, "reason": "TEMPLATE_INVALID"},
        ) from exc


async def execute_node(
    node: WorkflowNode,
    *,
    state: dict[str, Any],
    inputs: Mapping[str, Any],
    ctx: NodeExecutionContext,
    runner: WorkflowNodeRunner | None,
    warnings: list[str],
) -> NodeOutcome:
    """执行一个节点；异常向上抛（由引擎按 `on_error` 处理，4.5.2）。"""
    if node.type is NodeType.START:
        return NodeOutcome(output_summary={"input_keys": sorted(state_module.business_fields(state))})
    if node.type is NodeType.END:
        return NodeOutcome(output_summary={"state_keys": sorted(state_module.business_fields(state))})
    if node.type is NodeType.CONDITION:
        return _execute_condition(node, state=state, warnings=warnings)

    if node.type is NodeType.AGENT:
        outcome = await _require_runner(node, runner).run_agent_node(node, str(inputs.get("text") or ""), ctx)
    elif node.type is NodeType.TOOL:
        arguments = inputs.get("arguments")
        outcome = await _require_runner(node, runner).run_tool_node(node, dict(arguments or {}), ctx)
    elif node.type is NodeType.RETRIEVER:
        outcome = await _require_runner(node, runner).run_retriever_node(node, str(inputs.get("query") or ""), ctx)
    else:  # pragma: no cover - NodeType 已穷举
        raise WorkflowNodeFailedError(
            f"Unsupported node type '{node.type}'", details={"node_id": node.id, "node_type": str(node.type)}
        )
    outcome.output_summary = _apply_output(state, node, outcome)
    return outcome


def _execute_condition(node: WorkflowNode, *, state: dict[str, Any], warnings: list[str]) -> NodeOutcome:
    """按声明顺序求值 `branches[].when`，首个为真者胜；都不中则走 `default_next`。"""
    context = template_module.TemplateContext.from_state(state)
    for branch in node.branches:
        expression = template_module.normalize_expression(branch.when)
        if template_module.evaluate_condition(expression, context, warnings=warnings):
            return NodeOutcome(next_node_id=branch.next_node_id, output_summary={"matched": branch.when})
    if node.default_next:
        return NodeOutcome(next_node_id=node.default_next, output_summary={"matched": "default_next"})
    raise WorkflowNodeFailedError(
        f"No condition branch matched at node '{node.id}'",
        details={"node_id": node.id, "reason": "NO_BRANCH_MATCHED"},
    )
