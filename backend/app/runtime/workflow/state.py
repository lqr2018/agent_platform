"""Workflow state 的读写约定（详细设计 4.5.2）。

`state` 是**单一字典**，约定三个命名空间（4.5.2）：

```text
state.<业务字段>       用户输入与节点产物（模板里的 `state.*` 根）
nodes.<node_id>.output 只读访问任一节点输出（模板里的 `nodes.*` 根）
run.<run_id>           Run 级只读信息（模板里的 `run.*` 根）
```

- 业务字段与保留键**不会互相遮蔽**：模板里的 `state` 根只暴露业务字段
  （保留键 `nodes` / `run` 不在其中），要访问节点输出请用 `nodes.*`；
- `set_node_output()` 同时写 `state[output_key]` 与 `state["nodes"][node_id]["output"]`，
  这样"下游用 `{{state.plan}}`"与"下游用 `{{nodes.planner.output}}`"两种写法都成立；
- 写冲突按 4.5.2：**后写覆盖**，历史留在 `node_runs`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NODES_SCOPE = "nodes"
RUN_SCOPE = "run"
RESERVED_STATE_KEYS: frozenset[str] = frozenset({NODES_SCOPE, RUN_SCOPE})
"""保留键：不属于"业务字段"，不通过模板的 `state` 根暴露。"""

OUTPUT_FIELD = "output"


def initial_state(payload: Mapping[str, Any] | None = None, *, run: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造初始 state：`workflow_runs.input` 的业务字段 + `run` 命名空间。

    `payload` 里的 `nodes` / `run` 会被忽略（保留键由引擎维护，客户端不能直接写）。
    """
    state: dict[str, Any] = {key: value for key, value in dict(payload or {}).items() if key not in RESERVED_STATE_KEYS}
    state[NODES_SCOPE] = {}
    state[RUN_SCOPE] = dict(run or {})
    return state


def business_fields(state: Mapping[str, Any]) -> dict[str, Any]:
    """`state` 根视图：剔除保留键后的业务字段。"""
    return {key: value for key, value in state.items() if key not in RESERVED_STATE_KEYS}


def node_scope(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    """取（必要时创建）`nodes.<node_id>` 这一段。"""
    nodes = state.setdefault(NODES_SCOPE, {})
    if not isinstance(nodes, dict):
        nodes = {}
        state[NODES_SCOPE] = nodes
    scope = nodes.get(node_id)
    if not isinstance(scope, dict):
        scope = {}
        nodes[node_id] = scope
    return scope


def set_node_output(
    state: dict[str, Any],
    node_id: str,
    value: Any,
    *,
    output_key: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """写入节点产物：`state[output_key]` + `state.nodes.<node_id>.output`（4.5.2）。"""
    scope = node_scope(state, node_id)
    scope[OUTPUT_FIELD] = value
    if extra:
        scope.update(dict(extra))
    if output_key:
        state[output_key] = value


def node_scope_view(state: Mapping[str, Any]) -> dict[str, Any]:
    """`nodes` 根的只读视图（模板用；不存在时给空 dict）。"""
    raw = state.get(NODES_SCOPE)
    return dict(raw) if isinstance(raw, Mapping) else {}


def node_output(state: Mapping[str, Any], node_id: str) -> Any:
    """读节点输出（不存在 → `None`）。"""
    nodes = state.get(NODES_SCOPE)
    if not isinstance(nodes, Mapping):
        return None
    scope = nodes.get(node_id)
    if not isinstance(scope, Mapping):
        return None
    return scope.get(OUTPUT_FIELD)


def run_scope(state: Mapping[str, Any]) -> dict[str, Any]:
    """`run.*` 命名空间（只读）。"""
    raw = state.get(RUN_SCOPE)
    return dict(raw) if isinstance(raw, Mapping) else {}


def snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    """落库前的浅拷贝（`workflow_runs.state`）：避免把运行中的引用写进 DB。"""
    return dict(state)
