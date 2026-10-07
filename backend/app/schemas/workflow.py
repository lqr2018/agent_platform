"""Workflow DTO（详细设计 3.2.7 / 2.9）。

与 `tools.py` 的约定一致：**请求侧**（`WorkflowCreate` / `WorkflowUpdate` / `WorkflowRunCreate`）
与**响应侧**（`WorkflowRead` / `WorkflowRunRead` / `NodeRunRead`）分开；
`definition` 是"图的唯一事实来源"（2.9），因此原样进出（校验在服务层做，见 `graph.py`）。

`WorkflowValidateResult` 是"只校验不落库"（3.2.7）的响应：**即使不合法也回 200**，
把错误清单交给编辑器渲染；`graph` 是解析成功后的只读图投影（前端画简图用）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.schemas.common import UtcDatetime

WorkflowStatusLiteral = Literal["draft", "published", "archived"]
"""`workflows.status` 的三个取值（2.9 / `WorkflowStatus`）。"""


class WorkflowRead(BaseModel):
    """`workflows` 行（3.2.7 的列表 / 详情共用）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    version: int = 1
    status: str = "draft"
    start_node_id: str = ""
    definition: dict[str, Any] = Field(default_factory=dict)
    state_schema: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def node_count(self) -> int:
        """节点数（由 `definition.nodes` 现算，列表页直接展示）。"""
        nodes = self.definition.get("nodes")
        return len(nodes) if isinstance(nodes, list) else 0


class WorkflowCreate(BaseModel):
    """`POST /workflows`（3.2.7：`definition` 走图校验）。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4_000)
    definition: dict[str, Any]
    state_schema: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "published"] = "draft"
    """`archived` 只能通过 `PATCH` 归档；新建即发布允许（等价于建完立刻 publish）。"""

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class WorkflowUpdate(BaseModel):
    """`PATCH /workflows/{id}`：只传需要改的字段（同 `ToolUpdate` 的约定）。

    2.9：`definition` 变更后 `status` 回到 `draft`（已发布版本的语义不能悄悄漂移）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4_000)
    definition: dict[str, Any] | None = None
    state_schema: dict[str, Any] | None = None
    status: WorkflowStatusLiteral | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class WorkflowValidationIssue(BaseModel):
    """一条图校验错误（对应 `graph.GraphIssue`）。"""

    code: str
    message: str
    node_id: str | None = None


class WorkflowValidateResult(BaseModel):
    """`POST /workflows/{id}/validate`（3.2.7：只校验不落库）。"""

    valid: bool
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)
    graph: dict[str, Any] | None = None
    """解析成功时的只读图投影（`WorkflowGraph.public_definition()`），供前端画简图。"""


class WorkflowValidateRequest(BaseModel):
    """可选请求体：直接校验一份**未保存**的草稿（编辑器里"保存前先校验"）。"""

    definition: dict[str, Any] | None = None


class WorkflowRunCreate(BaseModel):
    """`POST /workflows/{id}/runs` 的请求（3.2.7：启动运行 → 202）。

    3.2.7 未规定请求体形状；这里定成 `input`（注入初始 state 的业务字段），
    inline Chat 场景由服务层自动注入 `input`（用户消息）与 `conversation_id`。
    """

    input: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunRead(BaseModel):
    """`workflow_runs` 行（3.2.7：详情含 `state` 与当前节点）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    workflow_version: int = 1
    status: str = "pending"
    trigger: str = "manual"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    current_node_id: str | None = None
    trace_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    latency_ms: int | None = None
    created_at: UtcDatetime


class NodeRunRead(BaseModel):
    """`node_runs` 行（3.2.7：节点执行记录，按 `seq`）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    node_id: str
    node_type: str
    name: str = ""
    seq: int = 0
    iteration: int = 1
    attempt: int = 1
    status: str = "running"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    agent_run_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    latency_ms: int | None = None
    created_at: UtcDatetime
