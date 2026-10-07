"""Workflow 图解析与静态校验（详细设计 4.5.1 / 4.5.2 / 2.9 / SD-1）。

`parse_definition()` 把 `workflows.definition`（JSON）解析成 `WorkflowGraph` 并做静态校验：

- 节点 id 唯一、`start` 恰好一个、至少有 `end`、所有 `next` / `branches[].next` / `default_next`
  可达、无孤立节点、`agent` 节点引用的 Agent 存在、**任一节点出边数 ≤ 1（SD-1）**；
- 校验失败 → `WorkflowInvalidGraphError`（422），错误清单在 `details.errors`
  （每项 `{code, message, node_id?}`；并行出边的单项 `code=PARALLEL_EDGES_NOT_SUPPORTED`）；
- 模板（`input_template` / `arguments_template` / `query_template` / `when`）在**保存时**就做一次
  静态检查（`template.check_*`），运行期不再吞掉"写错函数名"这类问题。

本模块**不 import db / services**（1.2）：`agent` / `tool` 节点的引用校验靠服务层传入的
`GraphReferences`（已知 id 集合），没传则跳过该条规则（单测里只验证图结构）。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.enums import NodeType
from app.core.errors import WorkflowInvalidGraphError
from app.db.models.workflow import definition_config
from app.runtime.workflow import template as template_module

NODE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
"""节点 id：小写开头，允许 `a-z0-9_-`，最长 64（`node_runs.node_id` 是 TEXT(64)）。"""

DEFAULT_AGENT_INPUT_TEMPLATE = "{{state.input}}"
"""`agent` / `retriever` 节点缺省 `input_template`：取初始输入（3.2.7 的 inline 场景注入 `input`）。"""

MAX_NODES = 200
"""节点数上限（串行图，200 已远超实际需要；防"配置里写了一个巨型图"）。"""


class GraphIssueCode(StrEnum):
    """图校验的错误项码（4.5.1；`PARALLEL_EDGES_NOT_SUPPORTED` 是 SD-1 的专项）。"""

    DEFINITION_NOT_OBJECT = "DEFINITION_NOT_OBJECT"
    NODES_MISSING = "NODES_MISSING"
    NODES_NOT_LIST = "NODES_NOT_LIST"
    NODES_EMPTY = "NODES_EMPTY"
    NODES_TOO_MANY = "NODES_TOO_MANY"
    NODE_NOT_OBJECT = "NODE_NOT_OBJECT"
    NODE_ID_MISSING = "NODE_ID_MISSING"
    NODE_ID_INVALID = "NODE_ID_INVALID"
    NODE_ID_DUPLICATE = "NODE_ID_DUPLICATE"
    NODE_TYPE_UNKNOWN = "NODE_TYPE_UNKNOWN"
    START_NODE_MISSING = "START_NODE_MISSING"
    START_NODE_DUPLICATED = "START_NODE_DUPLICATED"
    END_NODE_MISSING = "END_NODE_MISSING"
    END_NODE_HAS_NEXT = "END_NODE_HAS_NEXT"
    NEXT_MISSING = "NEXT_MISSING"
    NEXT_NOT_FOUND = "NEXT_NOT_FOUND"
    PARALLEL_EDGES_NOT_SUPPORTED = "PARALLEL_EDGES_NOT_SUPPORTED"
    EDGE_NOT_OBJECT = "EDGE_NOT_OBJECT"
    EDGE_ENDPOINT_MISSING = "EDGE_ENDPOINT_MISSING"
    EDGE_CONFLICT = "EDGE_CONFLICT"
    UNREACHABLE_NODE = "UNREACHABLE_NODE"
    ON_ERROR_INVALID = "ON_ERROR_INVALID"
    AGENT_REF_MISSING = "AGENT_REF_MISSING"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    NESTED_WORKFLOW_NOT_SUPPORTED = "NESTED_WORKFLOW_NOT_SUPPORTED"
    TOOL_NAME_MISSING = "TOOL_NAME_MISSING"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    RETRIEVER_KB_MISSING = "RETRIEVER_KB_MISSING"
    CONDITION_BRANCHES_MISSING = "CONDITION_BRANCHES_MISSING"
    CONDITION_BRANCH_NOT_OBJECT = "CONDITION_BRANCH_NOT_OBJECT"
    TEMPLATE_INVALID = "TEMPLATE_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"


@dataclass(frozen=True, slots=True)
class GraphIssue:
    """一条校验错误（`details.errors` 的元素）。"""

    code: GraphIssueCode | str
    message: str
    node_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.node_id:
            payload["node_id"] = self.node_id
        return payload


@dataclass(frozen=True, slots=True)
class OnErrorPolicy:
    """节点级失败策略（4.5.2）：`fail` / `continue` / `retry(n)`。"""

    mode: str
    retries: int = 0

    @property
    def max_attempts(self) -> int:
        """总尝试次数（首次 + 重试）。`retry(2)` → 3。"""
        return self.retries + 1

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "retries": self.retries}


@dataclass(frozen=True, slots=True)
class Branch:
    """`condition` 节点的一路分支（4.5.2）。"""

    when: str
    next_node_id: str
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    """一个已解析的节点（保留原始 `raw`，供前端只读图与回写）。"""

    id: str
    type: NodeType
    name: str
    raw: Mapping[str, Any]
    next_node_id: str | None = None
    branches: tuple[Branch, ...] = ()
    default_next: str | None = None
    on_error: OnErrorPolicy = OnErrorPolicy(mode="fail")
    seq: int = 0
    """节点在 `definition.nodes` 里的顺序（仅用于展示/诊断）。"""

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def str_field(self, key: str, default: str = "") -> str:
        value = self.raw.get(key)
        return value if isinstance(value, str) else default

    @property
    def output_key(self) -> str | None:
        value = self.raw.get("output_key")
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    """解析 + 校验后的图（引擎只认它，不再回头读 JSON）。"""

    definition: Mapping[str, Any]
    nodes: Mapping[str, WorkflowNode]
    start_node_id: str
    end_node_ids: tuple[str, ...]
    config: Mapping[str, Any]

    def node(self, node_id: str) -> WorkflowNode:
        node = self.nodes.get(node_id)
        if node is None:
            raise WorkflowInvalidGraphError(
                f"Node '{node_id}' does not exist in this graph", details={"node_id": node_id}
            )
        return node

    def next_node_id(self, node: WorkflowNode) -> str | None:
        """线性节点的下一个节点；`condition` 由执行器决定（返回 `None`）。"""
        if node.type == NodeType.CONDITION:
            return None
        return node.next_node_id

    def public_definition(self) -> dict[str, Any]:
        """给前端只读图用的精简定义（节点 + 解析后的出边）。"""
        return {
            "start_node_id": self.start_node_id,
            "end_node_ids": list(self.end_node_ids),
            "config": dict(self.config),
            "nodes": [
                {
                    "id": node.id,
                    "type": str(node.type),
                    "name": node.name,
                    "next": node.next_node_id,
                    "branches": [{"when": branch.when, "next": branch.next_node_id} for branch in node.branches],
                    "default_next": node.default_next,
                }
                for node in self.nodes.values()
            ],
        }


@dataclass(frozen=True, slots=True)
class GraphReferences:
    """`agent` / `tool` 节点引用校验所需的"已知集合"（服务层从 DB 装配，4.5.1）。"""

    agent_ids: Collection[str] = ()
    tool_names: Collection[str] = ()
    agent_workflow_ids: Mapping[str, str | None] = field(default_factory=dict)
    """`agent_id → workflow_id`：禁止"Workflow 里再套一个带 Workflow 的 Agent"（递归会失控）。"""

    def has_agent_scope(self) -> bool:
        """是否提供了 Agent 集合（未提供 → 跳过引用校验，单测只验图结构）。"""
        return bool(self.agent_ids)


ON_ERROR_PATTERN = re.compile(r"^retry\(\s*([1-9]\d*)\s*\)$")
MAX_RETRIES = 5
"""`retry(n)` 的上限：再多就该用 `condition` 回边表达循环，而不是原地重试。"""

EDGE_FROM_KEYS = ("from", "source")
EDGE_TO_KEYS = ("to", "target", "next")


@dataclass(slots=True)
class _RawNode:
    """解析中途的可变形态（出边归一化后再冻结成 `WorkflowNode`）。"""

    id: str
    type: NodeType
    name: str
    raw: Mapping[str, Any]
    index: int
    next_node_id: str | None
    branches: list[Branch]
    default_next: str | None
    on_error: OnErrorPolicy


def _default_on_error(node_type: NodeType) -> OnErrorPolicy:
    """4.5.2 的默认值：`agent` 默认 `fail`，`tool` / `retriever` 默认 `continue`。"""
    if node_type in (NodeType.TOOL, NodeType.RETRIEVER):
        return OnErrorPolicy(mode="continue")
    return OnErrorPolicy(mode="fail")


def _parse_on_error(raw: Mapping[str, Any], node_type: NodeType) -> tuple[OnErrorPolicy, list[GraphIssue]]:
    value = raw.get("on_error")
    if value is None:
        return _default_on_error(node_type), []
    if not isinstance(value, str):
        return _default_on_error(node_type), [
            GraphIssue(GraphIssueCode.ON_ERROR_INVALID, "on_error must be a string", str(raw.get("id") or ""))
        ]
    text = value.strip()
    if text == "fail":
        return OnErrorPolicy(mode="fail"), []
    if text == "continue":
        return OnErrorPolicy(mode="continue"), []
    match = ON_ERROR_PATTERN.match(text)
    if match and int(match.group(1)) <= MAX_RETRIES:
        return OnErrorPolicy(mode="retry", retries=int(match.group(1))), []
    return _default_on_error(node_type), [
        GraphIssue(
            GraphIssueCode.ON_ERROR_INVALID,
            f"unsupported on_error '{text}' (expect fail / continue / retry(n), n<= {MAX_RETRIES})",
            str(raw.get("id") or ""),
        )
    ]


def _parse_branches(raw: Mapping[str, Any], node_id: str) -> tuple[list[Branch], list[GraphIssue]]:
    issues: list[GraphIssue] = []
    raw_branches = raw.get("branches")
    if raw_branches is None:
        return [], issues
    if not isinstance(raw_branches, Sequence) or isinstance(raw_branches, (str, bytes)):
        return [], [GraphIssue(GraphIssueCode.CONDITION_BRANCH_NOT_OBJECT, "branches must be an array", node_id)]
    branches: list[Branch] = []
    for index, item in enumerate(raw_branches):
        if not isinstance(item, Mapping):
            issues.append(
                GraphIssue(GraphIssueCode.CONDITION_BRANCH_NOT_OBJECT, f"branches[{index}] must be an object", node_id)
            )
            continue
        when = item.get("when")
        target = item.get("next")
        if not isinstance(when, str) or not when.strip():
            issues.append(
                GraphIssue(GraphIssueCode.CONDITION_BRANCH_NOT_OBJECT, f"branches[{index}].when is required", node_id)
            )
            continue
        if not isinstance(target, str) or not target.strip():
            issues.append(
                GraphIssue(GraphIssueCode.CONDITION_BRANCH_NOT_OBJECT, f"branches[{index}].next is required", node_id)
            )
            continue
        error = template_module.check_expression(template_module.normalize_expression(when))
        if error:
            issues.append(GraphIssue(GraphIssueCode.TEMPLATE_INVALID, f"branches[{index}].when: {error}", node_id))
            continue
        branches.append(Branch(when=when.strip(), next_node_id=target.strip(), raw=dict(item)))
    return branches, issues


def _str_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _peek_id(raw: Any) -> str | None:
    """只取 id（用于"类型非法"的节点也参与重复检查）。"""
    if not isinstance(raw, Mapping):
        return None
    return _str_or_none(raw.get("id"))


def _parse_node(raw: Any, index: int) -> tuple[_RawNode | None, list[GraphIssue]]:
    """解析单个节点：id / 类型 / 类型专属必填项 / 模板静态检查 / `on_error`。"""
    if not isinstance(raw, Mapping):
        return None, [GraphIssue(GraphIssueCode.NODE_NOT_OBJECT, f"nodes[{index}] must be an object")]
    node_id = _str_or_none(raw.get("id"))
    if node_id is None:
        return None, [GraphIssue(GraphIssueCode.NODE_ID_MISSING, f"nodes[{index}].id is required")]
    if not NODE_ID_PATTERN.match(node_id):
        return None, [
            GraphIssue(
                GraphIssueCode.NODE_ID_INVALID,
                f"node id '{node_id}' must match {NODE_ID_PATTERN.pattern}",
                node_id,
            )
        ]
    try:
        node_type = NodeType(str(raw.get("type")))
    except ValueError:
        return None, [
            GraphIssue(
                GraphIssueCode.NODE_TYPE_UNKNOWN,
                f"unknown node type '{raw.get('type')}' (allowed: {', '.join(str(item) for item in NodeType)})",
                node_id,
            )
        ]

    issues: list[GraphIssue] = []
    for field_name, expect_mapping in (
        ("input_template", False),
        ("query_template", False),
        ("arguments_template", True),
    ):
        issues.extend(_check_template_field(raw, field_name, node_id, expect_mapping=expect_mapping))

    output_key = raw.get("output_key")
    if output_key is not None and _str_or_none(output_key) is None:
        issues.append(GraphIssue(GraphIssueCode.TEMPLATE_INVALID, "output_key must be a non-empty string", node_id))

    on_error, on_error_issues = _parse_on_error(raw, node_type)
    issues.extend(on_error_issues)

    next_node_id = _str_or_none(raw.get("next"))
    if raw.get("next") is not None and next_node_id is None:
        issues.append(GraphIssue(GraphIssueCode.NEXT_MISSING, "next must be a non-empty string", node_id))
    branches, branch_issues = _parse_branches(raw, node_id)
    issues.extend(branch_issues)
    default_next = _str_or_none(raw.get("default_next"))
    if raw.get("default_next") is not None and default_next is None:
        issues.append(GraphIssue(GraphIssueCode.NEXT_MISSING, "default_next must be a non-empty string", node_id))

    if node_type is NodeType.AGENT and _str_or_none(raw.get("agent_id")) is None:
        issues.append(GraphIssue(GraphIssueCode.AGENT_REF_MISSING, "agent nodes require agent_id", node_id))
    if node_type is NodeType.TOOL and _str_or_none(raw.get("tool_name")) is None:
        issues.append(GraphIssue(GraphIssueCode.TOOL_NAME_MISSING, "tool nodes require tool_name", node_id))
    if node_type is NodeType.RETRIEVER and _str_or_none(raw.get("kb_id")) is None:
        issues.append(GraphIssue(GraphIssueCode.RETRIEVER_KB_MISSING, "retriever nodes require kb_id", node_id))
    if node_type is NodeType.CONDITION and not branches and default_next is None:
        issues.append(
            GraphIssue(
                GraphIssueCode.CONDITION_BRANCHES_MISSING, "condition nodes require branches or default_next", node_id
            )
        )
    if node_type is NodeType.END and (next_node_id is not None or branches or default_next is not None):
        issues.append(GraphIssue(GraphIssueCode.END_NODE_HAS_NEXT, "end nodes cannot have outgoing edges", node_id))
    if node_type is NodeType.START and (branches or default_next is not None):
        issues.append(GraphIssue(GraphIssueCode.NEXT_MISSING, "start nodes use a single `next` (no branches)", node_id))

    name = raw.get("name")
    node_name = name.strip() if isinstance(name, str) and name.strip() else node_id
    return (
        _RawNode(
            id=node_id,
            type=node_type,
            name=node_name,
            raw=dict(raw),
            index=index,
            next_node_id=next_node_id,
            branches=branches,
            default_next=default_next,
            on_error=on_error,
        ),
        issues,
    )


def _parse_edges(raw_edges: Any, issues: list[GraphIssue]) -> dict[str, list[str]]:
    """`edges` 是 `next` 的**可选等价写法**（2.9）：元素 `{"from": ..., "to": ...}`。"""
    if raw_edges is None:
        return {}
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        issues.append(GraphIssue(GraphIssueCode.EDGE_NOT_OBJECT, "definition.edges must be an array"))
        return {}
    result: dict[str, list[str]] = {}
    for index, item in enumerate(raw_edges):
        if not isinstance(item, Mapping):
            issues.append(GraphIssue(GraphIssueCode.EDGE_NOT_OBJECT, f"edges[{index}] must be an object"))
            continue
        start = next((_str_or_none(item.get(key)) for key in EDGE_FROM_KEYS if _str_or_none(item.get(key))), None)
        target = next((_str_or_none(item.get(key)) for key in EDGE_TO_KEYS if _str_or_none(item.get(key))), None)
        if start is None or target is None:
            issues.append(
                GraphIssue(
                    GraphIssueCode.EDGE_ENDPOINT_MISSING,
                    f"edges[{index}] requires from/to node ids",
                )
            )
            continue
        result.setdefault(start, []).append(target)
    return result


def _check_template_field(
    raw: Mapping[str, Any], field_name: str, node_id: str, *, expect_mapping: bool = False
) -> list[GraphIssue]:
    """模板字段的存在性 + 静态检查（保存时就暴露"白名单外函数"这类错误）。"""
    value = raw.get(field_name)
    if value is None:
        return []
    if expect_mapping and not isinstance(value, Mapping):
        return [GraphIssue(GraphIssueCode.TEMPLATE_INVALID, f"{field_name} must be an object", node_id)]
    if not expect_mapping and not isinstance(value, str):
        return [GraphIssue(GraphIssueCode.TEMPLATE_INVALID, f"{field_name} must be a string", node_id)]
    return [
        GraphIssue(GraphIssueCode.TEMPLATE_INVALID, f"{field_name}: {problem}", node_id)
        for problem in template_module.check_value(value)
    ]


def validate_definition(definition: Any, *, references: GraphReferences | None = None) -> list[GraphIssue]:
    """收集**全部**校验问题（空列表 = 合法）。4.5.1 的规则 + SD-1 的并行出边拒绝。"""
    if not isinstance(definition, Mapping):
        return [GraphIssue(GraphIssueCode.DEFINITION_NOT_OBJECT, "definition must be a JSON object")]
    raw_nodes = definition.get("nodes")
    if raw_nodes is None:
        return [GraphIssue(GraphIssueCode.NODES_MISSING, "definition.nodes is required")]
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        return [GraphIssue(GraphIssueCode.NODES_NOT_LIST, "definition.nodes must be an array")]
    if not raw_nodes:
        return [GraphIssue(GraphIssueCode.NODES_EMPTY, "definition.nodes must not be empty")]

    issues: list[GraphIssue] = []
    if len(raw_nodes) > MAX_NODES:
        issues.append(GraphIssue(GraphIssueCode.NODES_TOO_MANY, f"at most {MAX_NODES} nodes are supported"))

    seen: set[str] = set()
    parsed: list[_RawNode] = []
    for index, raw in enumerate(raw_nodes):
        node, node_issues = _parse_node(raw, index)
        node_id = node.id if node is not None else _peek_id(raw)
        if node_id:
            if node_id in seen:
                issues.append(GraphIssue(GraphIssueCode.NODE_ID_DUPLICATE, f"duplicate node id '{node_id}'", node_id))
            else:
                seen.add(node_id)
        issues.extend(node_issues)
        if node is not None:
            parsed.append(node)

    edge_map = _parse_edges(definition.get("edges"), issues)
    starts = [node for node in parsed if node.type is NodeType.START]
    ends = [node for node in parsed if node.type is NodeType.END]
    if not starts:
        issues.append(GraphIssue(GraphIssueCode.START_NODE_MISSING, "exactly one `start` node is required"))
    elif len(starts) > 1:
        issues.append(
            GraphIssue(GraphIssueCode.START_NODE_DUPLICATED, "exactly one `start` node is required", starts[1].id)
        )
    if not ends:
        issues.append(GraphIssue(GraphIssueCode.END_NODE_MISSING, "at least one `end` node is required"))

    outgoing: dict[str, list[str]] = {}
    for node in parsed:
        if node.type is NodeType.CONDITION:
            # `condition` 的出边是"每路 1 条"（4.5.2）：branches + default_next 不算并行（SD-1 豁免）
            targets = [branch.next_node_id for branch in node.branches]
            if node.default_next:
                targets.append(node.default_next)
            outgoing[node.id] = sorted(set(targets))
            continue
        targets = list(edge_map.get(node.id, []))
        if node.next_node_id:
            if edge_map.get(node.id) and set(edge_map[node.id]) != {node.next_node_id}:
                issues.append(
                    GraphIssue(
                        GraphIssueCode.EDGE_CONFLICT,
                        f"`next` ({node.next_node_id}) conflicts with `edges` for node '{node.id}'",
                        node.id,
                    )
                )
            targets.append(node.next_node_id)
        unique_targets = sorted(set(targets))
        if len(unique_targets) > 1:
            issues.append(
                GraphIssue(
                    GraphIssueCode.PARALLEL_EDGES_NOT_SUPPORTED,
                    f"node '{node.id}' has {len(unique_targets)} outgoing edges; "
                    "parallel branches are not supported (SD-1)",
                    node.id,
                )
            )
        outgoing[node.id] = unique_targets
        if node.type is not NodeType.END and not unique_targets:
            issues.append(GraphIssue(GraphIssueCode.NEXT_MISSING, f"node '{node.id}' requires a next node", node.id))

    for node_id, targets in outgoing.items():
        for target in targets:
            if target not in seen:
                issues.append(
                    GraphIssue(GraphIssueCode.NEXT_NOT_FOUND, f"next node '{target}' does not exist", node_id)
                )

    if starts:
        reachable: set[str] = set()
        stack = [starts[0].id]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(outgoing.get(current, []))
        for node in parsed:
            if node.id not in reachable:
                issues.append(
                    GraphIssue(
                        GraphIssueCode.UNREACHABLE_NODE, f"node '{node.id}' is not reachable from start", node.id
                    )
                )

    issues.extend(_reference_issues(parsed, references))
    issues.extend(_config_issues(definition))
    return issues


def _reference_issues(parsed: Sequence[_RawNode], references: GraphReferences | None) -> list[GraphIssue]:
    """`agent` / `tool` 节点的引用校验（4.5.1）；未提供已知集合时跳过。"""
    if references is None:
        return []
    issues: list[GraphIssue] = []
    known_agents = set(references.agent_ids)
    known_tools = set(references.tool_names)
    for node in parsed:
        if node.type is NodeType.AGENT:
            agent_id = _str_or_none(node.raw.get("agent_id"))
            if agent_id is None:
                continue
            if known_agents and agent_id not in known_agents:
                issues.append(
                    GraphIssue(
                        GraphIssueCode.AGENT_NOT_FOUND,
                        f"agent '{agent_id}' does not exist or is disabled",
                        node.id,
                    )
                )
                continue
            if references.agent_workflow_ids.get(agent_id):
                issues.append(
                    GraphIssue(
                        GraphIssueCode.NESTED_WORKFLOW_NOT_SUPPORTED,
                        f"agent '{agent_id}' is bound to a workflow; nested workflows are not supported (4.4.4)",
                        node.id,
                    )
                )
        elif node.type is NodeType.TOOL:
            tool_name = _str_or_none(node.raw.get("tool_name"))
            if tool_name and known_tools and tool_name not in known_tools:
                issues.append(
                    GraphIssue(
                        GraphIssueCode.TOOL_NOT_FOUND,
                        f"tool '{tool_name}' does not exist or is disabled",
                        node.id,
                    )
                )
    return issues


def _config_issues(definition: Mapping[str, Any]) -> list[GraphIssue]:
    """`definition.config` 的三个数值项必须为正整数（4.5.2）。"""
    raw_config = definition.get("config")
    if raw_config is not None and not isinstance(raw_config, Mapping):
        return [GraphIssue(GraphIssueCode.CONFIG_INVALID, "definition.config must be an object")]
    resolved = definition_config(dict(definition))
    issues: list[GraphIssue] = []
    for key in ("max_steps", "recursion_limit", "timeout_seconds"):
        value = resolved.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            issues.append(
                GraphIssue(GraphIssueCode.CONFIG_INVALID, f"config.{key} must be a positive integer (got {value!r})")
            )
    return issues


def parse_definition(definition: Any, *, references: GraphReferences | None = None) -> WorkflowGraph:
    """解析 + 校验；失败抛 `WorkflowInvalidGraphError`（422，`details.errors` 带清单）。"""
    issues = validate_definition(definition, references=references)
    if issues:
        raise WorkflowInvalidGraphError(
            "Workflow definition is not a valid graph",
            details={
                "errors": [issue.as_dict() for issue in issues],
                "rules": "4.5.1（唯一 id / 一个 start / 至少一个 end / 可达 / 无孤立节点 / 引用存在 / 禁止并行出边）",
            },
        )
    if not isinstance(definition, Mapping):  # pragma: no cover - validate_definition 已拦下
        raise WorkflowInvalidGraphError("definition must be a JSON object")
    edge_map = _parse_edges(definition.get("edges"), [])
    nodes: dict[str, WorkflowNode] = {}
    for index, raw in enumerate(definition.get("nodes") or []):
        parsed, _ = _parse_node(raw, index)
        if parsed is None:  # pragma: no cover - validate_definition 已拦下
            raise WorkflowInvalidGraphError("node could not be parsed")
        next_node_id = parsed.next_node_id
        if next_node_id is None and parsed.type is not NodeType.CONDITION:
            # 2.9：`edges` 是 `next` 的等价写法 —— 归一化到 `next_node_id`，引擎只认一种形态
            candidates = sorted(set(edge_map.get(parsed.id, [])))
            next_node_id = candidates[0] if len(candidates) == 1 else None
        nodes[parsed.id] = WorkflowNode(
            id=parsed.id,
            type=parsed.type,
            name=parsed.name,
            raw=parsed.raw,
            next_node_id=next_node_id,
            branches=tuple(parsed.branches),
            default_next=parsed.default_next,
            on_error=parsed.on_error,
            seq=parsed.index,
        )
    starts = [node for node in nodes.values() if node.type is NodeType.START]
    ends = tuple(node.id for node in nodes.values() if node.type is NodeType.END)
    return WorkflowGraph(
        definition=dict(definition),
        nodes=nodes,
        start_node_id=starts[0].id,
        end_node_ids=ends,
        config=definition_config(dict(definition)),
    )
