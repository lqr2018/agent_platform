"""图解析与静态校验单测（详细设计 4.5.1 / 7.4 测试要求）。

覆盖 7 类非法输入：重复 id、多 start、无 end、不可达、孤立节点、缺失 agent 引用、
**一个节点多条无条件出边 → `PARALLEL_EDGES_NOT_SUPPORTED`**（SD-1），
外加 `edges` 等价写法、模板静态检查与 `on_error` 解析。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.core.errors import ErrorCode, WorkflowInvalidGraphError
from app.db.models.workflow import definition_config
from app.runtime.workflow import GraphReferences
from app.runtime.workflow.graph import GraphIssueCode, parse_definition, validate_definition
from tests.unit.workflow.stubs import canonical_definition


def codes(definition: Any, *, references: GraphReferences | None = None) -> set[str]:
    return {str(issue.code) for issue in validate_definition(definition, references=references)}


def mutated(**changes: Any) -> dict[str, Any]:
    """在标准图上打补丁（`nodes` 深拷贝，避免测试之间互相污染）。"""
    definition = copy.deepcopy(canonical_definition())
    definition.update(changes)
    return definition


def with_nodes(*extra: dict[str, Any]) -> dict[str, Any]:
    definition = copy.deepcopy(canonical_definition())
    definition["nodes"].extend(extra)
    return definition


def test_valid_canonical_definition_parses() -> None:
    """合法图：start 定位、节点索引、config 默认值与公开（只读图）视图。"""
    graph = parse_definition(canonical_definition())
    assert graph.start_node_id == "start"
    assert list(graph.nodes) == ["start", "planner", "search", "router", "writer", "end"]
    assert graph.config["max_steps"] == 10
    assert graph.node("router").branches[0].next_node_id == "writer"
    public = graph.public_definition()
    assert public["start_node_id"] == "start"
    assert public["nodes"][1]["next"] == "search"


def test_config_defaults_when_absent() -> None:
    """`config` 缺省 → 4.5.2 的 30 / 50 / 600（`db/models/workflow.definition_config`）。"""
    definition = canonical_definition()
    definition.pop("config")
    config = definition_config(definition)
    assert (config["max_steps"], config["recursion_limit"], config["timeout_seconds"]) == (30, 50, 600)
    assert parse_definition(definition).config["max_steps"] == 30


def test_duplicate_node_id_is_rejected() -> None:
    definition = with_nodes({"id": "end", "type": "end"})
    assert str(GraphIssueCode.NODE_ID_DUPLICATE) in codes(definition)


def test_multiple_start_nodes_are_rejected() -> None:
    definition = mutated(nodes=[*canonical_definition()["nodes"], {"id": "start2", "type": "start", "next": "end"}])
    assert str(GraphIssueCode.START_NODE_DUPLICATED) in codes(definition)


def test_missing_end_node_is_rejected() -> None:
    definition = canonical_definition()
    definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "end"]
    assert str(GraphIssueCode.END_NODE_MISSING) in codes(definition)


def test_unreachable_orphan_node_is_rejected() -> None:
    definition = with_nodes({"id": "lonely", "type": "agent", "agent_id": "agt_x", "next": "end"})
    assert str(GraphIssueCode.UNREACHABLE_NODE) in codes(definition)


def test_missing_agent_reference_is_rejected() -> None:
    """4.5.1：`agent` 节点引用的 Agent 必须存在（服务层把已知集合传进来）。"""
    references = GraphReferences(agent_ids={"agt_planner"}, tool_names={"web_search"})
    assert str(GraphIssueCode.AGENT_NOT_FOUND) in codes(canonical_definition(), references=references)

    complete = GraphReferences(agent_ids={"agt_planner", "agt_writer"}, tool_names={"web_search"})
    assert codes(canonical_definition(), references=complete) == set()


def test_reference_checks_are_skipped_without_scope() -> None:
    """未提供已知集合（纯图结构单测）→ 跳过引用校验，但结构问题照样报。"""
    assert codes(canonical_definition()) == set()


def test_nested_workflow_agent_is_rejected() -> None:
    """4.4.4：Workflow 里的 `agent` 节点不能指向自身绑定了 Workflow 的 Agent（否则递归失控）。"""
    references = GraphReferences(
        agent_ids={"agt_planner", "agt_writer"},
        tool_names={"web_search"},
        agent_workflow_ids={"agt_writer": "wf-inner"},
    )
    assert str(GraphIssueCode.NESTED_WORKFLOW_NOT_SUPPORTED) in codes(canonical_definition(), references=references)


def test_unknown_tool_is_rejected() -> None:
    references = GraphReferences(agent_ids={"agt_planner", "agt_writer"}, tool_names={"calculator"})
    assert str(GraphIssueCode.TOOL_NOT_FOUND) in codes(canonical_definition(), references=references)


@pytest.mark.parametrize(
    "edges",
    [
        pytest.param([{"from": "writer", "to": "writer"}, {"from": "writer", "to": "end"}], id="agent-node"),
        pytest.param([{"from": "planner", "to": "search"}, {"from": "planner", "to": "end"}], id="edges-conflict"),
    ],
)
def test_parallel_edges_are_rejected(edges: list[dict[str, str]]) -> None:
    """SD-1：一个节点多条无条件出边 → `PARALLEL_EDGES_NOT_SUPPORTED`（`edges` 也绕不过去）。"""
    definition = mutated(edges=edges)
    issues = validate_definition(definition)
    assert str(GraphIssueCode.PARALLEL_EDGES_NOT_SUPPORTED) in {str(issue.code) for issue in issues}
    with pytest.raises(WorkflowInvalidGraphError) as caught:
        parse_definition(definition)
    assert caught.value.code is ErrorCode.WORKFLOW_INVALID_GRAPH
    assert str(GraphIssueCode.PARALLEL_EDGES_NOT_SUPPORTED) in {item["code"] for item in caught.value.details["errors"]}


def test_edges_are_an_equivalent_form_of_next() -> None:
    """2.9：`edges` 是 `next` 的等价写法（节点里不写 `next` 也能解析）。"""
    definition = canonical_definition()
    for node in definition["nodes"]:
        node.pop("next", None)
    definition["edges"] = [
        {"from": "start", "to": "planner"},
        {"from": "planner", "to": "search"},
        {"from": "search", "to": "router"},
        {"from": "writer", "to": "end"},
    ]
    graph = parse_definition(definition)
    assert graph.node("planner").next_node_id == "search"
    assert graph.node("search").next_node_id == "router"


def test_edges_conflicting_with_next_are_rejected() -> None:
    definition = mutated(edges=[{"from": "search", "to": "end"}])
    assert str(GraphIssueCode.EDGE_CONFLICT) in codes(definition)


def test_missing_next_is_rejected() -> None:
    definition = canonical_definition()
    planner = next(node for node in definition["nodes"] if node["id"] == "planner")
    planner.pop("next")
    assert str(GraphIssueCode.NEXT_MISSING) in codes(definition)


def test_next_to_unknown_node_is_rejected() -> None:
    definition = canonical_definition()
    planner = next(node for node in definition["nodes"] if node["id"] == "planner")
    planner["next"] = "ghost"
    assert str(GraphIssueCode.NEXT_NOT_FOUND) in codes(definition)


def test_end_node_with_next_is_rejected() -> None:
    definition = canonical_definition()
    end = next(node for node in definition["nodes"] if node["id"] == "end")
    end["next"] = "start"
    assert str(GraphIssueCode.END_NODE_HAS_NEXT) in codes(definition)


def test_condition_without_branches_is_rejected() -> None:
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    router = next(node for node in definition["nodes"] if node["id"] == "router")
    router.pop("branches")
    router.pop("default_next")
    assert str(GraphIssueCode.CONDITION_BRANCHES_MISSING) in codes(definition)


def test_agent_node_requires_agent_id() -> None:
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    planner = next(node for node in definition["nodes"] if node["id"] == "planner")
    planner.pop("agent_id")
    assert str(GraphIssueCode.AGENT_REF_MISSING) in codes(definition)


def test_tool_node_requires_tool_name() -> None:
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    search = next(node for node in definition["nodes"] if node["id"] == "search")
    search.pop("tool_name")
    assert str(GraphIssueCode.TOOL_NAME_MISSING) in codes(definition)


def test_retriever_node_requires_kb_id() -> None:
    definition = mutated(
        nodes=[
            *[dict(node) for node in canonical_definition()["nodes"]],
            {"id": "reader", "type": "retriever", "output_key": "chunks", "next": "end"},
        ]
    )
    assert str(GraphIssueCode.RETRIEVER_KB_MISSING) in codes(definition)


def test_invalid_templates_are_rejected_at_save_time() -> None:
    """4.5.1 + 4.5.2：白名单外的函数 / 未知根在**保存时**就被拦下（不必等到运行期）。"""
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    planner = next(node for node in definition["nodes"] if node["id"] == "planner")
    planner["input_template"] = "{{state.plan.upper()}}"
    search = next(node for node in definition["nodes"] if node["id"] == "search")
    search["arguments_template"] = {"query": "{{payload.query}}"}
    writer = next(node for node in definition["nodes"] if node["id"] == "writer")
    writer["input_template"] = "{{json.loads(state.hits)}}"
    assert codes(definition) == {str(GraphIssueCode.TEMPLATE_INVALID)}


def test_invalid_condition_expression_is_rejected() -> None:
    """`when` 里的非白名单函数 → TEMPLATE_INVALID；该分支被丢弃后 writer 也不可达。"""
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    router = next(node for node in definition["nodes"] if node["id"] == "router")
    router["branches"] = [{"when": "eval(state.hits)", "next": "writer"}]
    assert codes(definition) == {str(GraphIssueCode.TEMPLATE_INVALID), str(GraphIssueCode.UNREACHABLE_NODE)}


def test_non_object_arguments_template_is_rejected() -> None:
    definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
    search = next(node for node in definition["nodes"] if node["id"] == "search")
    search["arguments_template"] = "{{state.plan}}"
    assert str(GraphIssueCode.TEMPLATE_INVALID) in codes(definition)


def test_invalid_config_is_rejected() -> None:
    assert str(GraphIssueCode.CONFIG_INVALID) in codes(canonical_definition(config={"max_steps": 0}))
    assert str(GraphIssueCode.CONFIG_INVALID) in codes(canonical_definition(config={"max_steps": "ten"}))
    assert str(GraphIssueCode.CONFIG_INVALID) in codes(mutated(config=[1, 2, 3]))


def test_definition_must_be_an_object_with_nodes() -> None:
    assert codes(["nope"]) == {str(GraphIssueCode.DEFINITION_NOT_OBJECT)}
    assert codes({"nodes": "nope"}) == {str(GraphIssueCode.NODES_NOT_LIST)}
    assert codes({}) == {str(GraphIssueCode.NODES_MISSING)}
    assert codes({"nodes": []}) == {str(GraphIssueCode.NODES_EMPTY)}
    assert str(GraphIssueCode.NODE_NOT_OBJECT) in codes({"nodes": ["nope"]})


def test_unknown_node_type_and_invalid_node_id() -> None:
    assert str(GraphIssueCode.NODE_TYPE_UNKNOWN) in codes(with_nodes({"id": "hook", "type": "human", "next": "end"}))
    assert str(GraphIssueCode.NODE_ID_INVALID) in codes(with_nodes({"id": "Bad Id", "type": "end"}))


def test_on_error_defaults_and_overrides() -> None:
    """4.5.2：`agent` 默认 `fail`，`tool` / `retriever` 默认 `continue`；`retry(n)` 解析。"""
    definition = canonical_definition(config=None)
    definition["nodes"] = [dict(node) for node in definition["nodes"]]
    search = next(node for node in definition["nodes"] if node["id"] == "search")
    search["on_error"] = "retry(2)"
    planner = next(node for node in definition["nodes"] if node["id"] == "planner")
    planner["on_error"] = "continue"
    graph = parse_definition(definition)
    assert graph.node("planner").on_error.mode == "continue"
    assert graph.node("search").on_error.max_attempts == 3
    assert graph.node("router").on_error.mode == "fail"

    default_graph = parse_definition(canonical_definition())
    assert default_graph.node("planner").on_error.mode == "fail"
    assert default_graph.node("search").on_error.mode == "continue"


def test_invalid_on_error_is_rejected() -> None:
    for value in ("retry(0)", "retry(9)", "ignore", "retry"):
        definition = mutated(nodes=[dict(node) for node in canonical_definition()["nodes"]])
        search = next(node for node in definition["nodes"] if node["id"] == "search")
        search["on_error"] = value
        assert str(GraphIssueCode.ON_ERROR_INVALID) in codes(definition), value
