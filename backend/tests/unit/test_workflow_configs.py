"""`configs/workflows/*.yaml` 的一致性与可用性单测（详细设计 7.4 交付物 / 8.2 / 8.4）。

两件事分开钉：

1. **文件本身**：每个交付的示例图都能被 `workflow_config` 装载，且通过 `graph` 的结构校验
   （`agent_id` / `tool_name` / `kb_id` 是占位名，引用校验属 Phase 8 的写库路径）；
2. **loader 行为**：schema 不合法（缺 `workflow` / 未知字段）与图非法（多出边）必须报错，
   避免"文件写错了但没人发现"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.errors import WorkflowInvalidGraphError
from app.runtime.workflow import parse_definition
from app.services import workflow_config

EXPECTED_NAMES = ["kb-qa-flow", "research-flow"]
"""7.4 交付物：`configs/workflows/*.yaml`（含第 8 章的 2 个示例图）。"""


def test_shipped_configs_load_and_validate() -> None:
    configs = workflow_config.load_workflow_configs()
    assert workflow_config.config_names(configs) == EXPECTED_NAMES
    for config in configs:
        graph = parse_definition(config.workflow.definition)
        assert graph.start_node_id == "start"
        assert "end" in graph.end_node_ids
        assert graph.config["max_steps"] >= 1
        assert config.version == 1


def test_shipped_configs_only_use_supported_node_types() -> None:
    """SD-17：示例图里不出现 `human`（Backlog）；`retriever` 允许出现但标注 Phase 5。"""
    from app.core.enums import NodeType

    allowed = {str(item) for item in NodeType}
    for config in workflow_config.load_workflow_configs():
        types = {str(node["type"]) for node in config.workflow.definition["nodes"]}
        assert types <= allowed
        assert "human" not in types


def test_load_workflow_config_rejects_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 1\nunknown: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        workflow_config.load_workflow_config(path)


def test_load_workflow_configs_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert workflow_config.load_workflow_configs(tmp_path / "missing") == []


def test_duplicate_edges_to_the_same_target_are_fine(tmp_path: Path) -> None:
    """2.9：同一条出边写两遍不算"并行"（SD-1 只拒绝**不同目标**的多出边）。"""
    path = tmp_path / "duplicate-edges.yaml"
    path.write_text(
        """
version: 1
workflow:
  name: duplicate-edges
  definition:
    nodes:
      - {id: start, type: start, next: end}
      - {id: end, type: end}
    edges:
      - {from: start, to: end}
      - {from: start, to: end}
""",
        encoding="utf-8",
    )
    config = workflow_config.load_workflow_config(path)
    assert config.workflow.name == "duplicate-edges"


def test_invalid_graph_in_config_is_rejected(tmp_path: Path) -> None:
    """`parse_definition` 的失败必须能从装载路径冒出来（不留"装载成功、运行才炸"的坑）。"""
    path = tmp_path / "parallel.yaml"
    path.write_text(
        """
version: 1
workflow:
  name: parallel
  definition:
    nodes:
      - {id: start, type: start, next: first}
      - {id: first, type: agent, agent_id: a1, next: end}
      - {id: second, type: agent, agent_id: a2, next: end}
      - {id: end, type: end}
    edges:
      - {from: start, to: first}
      - {from: start, to: second}
""",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowInvalidGraphError):
        workflow_config.load_workflow_config(path)
