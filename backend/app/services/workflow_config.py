"""`configs/workflows/*.yaml` 的读取与结构校验（详细设计 7.4 交付物 / 8.1 / 8.2）。

Phase 3 的范围：**读取 + 结构校验**（文件里的图必须通过 `graph.parse_definition` 的规则）。
Phase 8 的 `seed_demo`（8.1）复用本模块把示例图写进 `workflows` 表 —— 那时才需要 DB
（`agent_id` / `tool_name` 的引用校验由 `workflow_service.create_workflow` 完成）。

文件 schema（`WorkflowConfigFile`，Pydantic 强校验）：

```yaml
version: 1
workflow:
  name: research-flow
  description: "多步研究：规划 → 检索 → 撰写"
  state_schema: { topic: { type: string } }
  definition: { nodes: [...], config: {...} }
```
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.runtime.workflow import parse_definition

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "workflows"
"""仓库内默认目录（`backend/configs/workflows`，7.4 交付物）。"""

CONFIG_SUFFIXES = (".yaml", ".yml")


class WorkflowConfigBody(BaseModel):
    """`workflow:` 段（8.1 的 Agent 配置同款风格）。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4_000)
    state_schema: dict[str, Any] = Field(default_factory=dict)
    definition: dict[str, Any]


class WorkflowConfigFile(BaseModel):
    """一个 YAML 文件 = 一个 Workflow（`version` 预留给后续格式演进）。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    workflow: WorkflowConfigBody


def load_workflow_config(path: Path) -> WorkflowConfigFile:
    """读取单个文件；结构或**图规则**不合法时抛异常（`pydantic.ValidationError` / `WORKFLOW_INVALID_GRAPH`）。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = WorkflowConfigFile.model_validate(raw)
    parse_definition(config.workflow.definition)  # 结构校验（引用校验留给写库路径）
    return config


def list_workflow_config_paths(directory: Path | None = None) -> list[Path]:
    """目录内的配置文件（按文件名排序，便于测试稳定性）。"""
    base = directory or CONFIG_DIR
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir() if path.suffix in CONFIG_SUFFIXES)


def load_workflow_configs(directory: Path | None = None) -> list[WorkflowConfigFile]:
    """装载目录内的全部示例图（空目录 → 空列表）。"""
    return [load_workflow_config(path) for path in list_workflow_config_paths(directory)]


def config_names(configs: Sequence[WorkflowConfigFile]) -> list[str]:
    """便捷取值（`seed_demo` 与测试都要用名字做幂等键）。"""
    return [config.workflow.name for config in configs]
