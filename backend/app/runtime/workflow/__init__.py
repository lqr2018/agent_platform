"""Workflow 引擎（详细设计 4.5 / 7.4）。

| 模块 | 职责 |
|---|---|
| `graph` | `definition` JSON → `WorkflowGraph` + 静态校验（含 SD-1 的"禁止并行出边"） |
| `template` | 受限模板求值（白名单函数 `len`/`str`/`join`/`json.dumps`，**禁 `eval`**） |
| `state` | state 的三个命名空间（`state.*` / `nodes.*` / `run.*`，4.5.2） |
| `nodes` | 六类节点的执行语义（`start/agent/tool/retriever/condition/end`，**无 `human`**） |
| `checkpoint` | 引擎无关的断点结构 + resume 判定（4.5.4 / SD-10） |
| `base` | `WorkflowEngine` Protocol + 注入点（`WorkflowRunSink` / `WorkflowNodeRunner`） |
| `simple_engine` | 唯一引擎 `SimpleEngine`（SD-10：**不创建** `langgraph_engine.py`） |
"""

from __future__ import annotations

from app.runtime.workflow import checkpoint, graph, nodes, state, template
from app.runtime.workflow.base import (
    NodeExecutionContext,
    NodeOutcome,
    NodeRunFinished,
    NodeRunStarted,
    NullWorkflowRunSink,
    WorkflowEngine,
    WorkflowNodeRunner,
    WorkflowResult,
    WorkflowRunSink,
)
from app.runtime.workflow.graph import (
    Branch,
    GraphIssue,
    GraphIssueCode,
    GraphReferences,
    OnErrorPolicy,
    WorkflowGraph,
    WorkflowNode,
    parse_definition,
    validate_definition,
)
from app.runtime.workflow.simple_engine import SimpleEngine

__all__ = [
    "Branch",
    "GraphIssue",
    "GraphIssueCode",
    "GraphReferences",
    "NodeExecutionContext",
    "NodeOutcome",
    "NodeRunFinished",
    "NodeRunStarted",
    "NullWorkflowRunSink",
    "OnErrorPolicy",
    "SimpleEngine",
    "WorkflowEngine",
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowNodeRunner",
    "WorkflowResult",
    "WorkflowRunSink",
    "checkpoint",
    "graph",
    "nodes",
    "parse_definition",
    "state",
    "template",
    "validate_definition",
]
