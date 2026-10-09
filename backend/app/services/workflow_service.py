"""Workflow 服务：CRUD / 图校验 / 运行编排 / 落库（详细设计 3.2.7 / 4.5 / 7.4）。

分层位置：服务层是 `workflow_runs` / `node_runs` 与 runtime 之间**唯一**的适配点（1.2）：

1. **CRUD 与校验**：`definition` 每次写入都过 `graph.parse_definition`（含引用校验）；
2. **编排**：`start_run` 落 `workflow_runs` + `runs` + `traces`，再起后台任务跑
   `SimpleEngine`（与 `chat_service` 同一套注入风格：独立 session + `Tracer` 落库）；
3. **落库**：`DatabaseWorkflowRunSink` 实现 `WorkflowRunSink`（4.5.3 的 `node_runs` /
   `state` / `checkpoint` 每条一次写入）；
4. **节点 IO**：`ServiceNodeRunner` 实现 `WorkflowNodeRunner` —— `agent` 节点复用
   `AgentRuntime.run()`（4.4.4），`tool` 节点复用九步流水线，`retriever` 节点复用
   `kb_service.retrieve()`（4.5.2 / 4.6.3，Phase 5 接入）；
5. **收敛**：`converge_orphan_runs` 给 6.3 第 3 条（进程重启后的孤儿 Run）用。

**不返回 SSE**（3.4）：`POST /workflows/{id}/runs` 返回 202，前端轮询
`GET /workflow-runs/{id}/node-runs`；只有 Chat 内联触发（`agent.workflow_id` 非空）才走 SSE，
那时由 `chat_service` 传入 `emit_queue`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings
from app.core.enums import RunKind, RunStatus, SpanStatus, WorkflowStatus
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    InternalError,
    RunAlreadyFinishedError,
    RunCanceledError,
    RunTimeoutError,
    WorkflowInvalidGraphError,
    WorkflowNodeError,
    WorkflowNotFoundError,
    WorkflowRunNotFoundError,
    WorkflowRunNotResumableError,
)
from app.core.events import (
    RetrievalCompletedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunStartedPayload,
    SseEventType,
    SseQueueItem,
)
from app.core.logging import get_logger
from app.db.models import Agent, NodeRun, Run, Tool, Workflow, WorkflowRun
from app.db.models.agent import AGENT_STATUS_ENABLED
from app.db.models.tool import TOOL_STATUS_ENABLED
from app.db.models.workflow import TRIGGER_MANUAL, definition_config
from app.db.session import get_sessionmaker
from app.runtime.agent.emitter import EventEmitter, NullEmitter, QueueEmitter
from app.runtime.agent.runtime import AgentRuntime
from app.runtime.llm.base import ToolCallSpec
from app.runtime.llm.registry import LLMRegistry
from app.runtime.memory.base import ShortTermMemory
from app.runtime.memory.stateless import InMemoryShortTermMemory
from app.runtime.observability.tracer import RunTrace, Tracer
from app.runtime.tools.executor import ToolExecutor, ToolRunContext
from app.runtime.tools.registry import default_registry
from app.runtime.workflow import (
    GraphReferences,
    NodeExecutionContext,
    NodeOutcome,
    NodeRunFinished,
    NodeRunStarted,
    SimpleEngine,
    WorkflowGraph,
    WorkflowNode,
    WorkflowResult,
    parse_definition,
)
from app.runtime.workflow import graph as graph_module
from app.runtime.workflow import state as state_module
from app.runtime.workflow import template as template_module
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate
from app.services import (
    agent_service,
    conversation_service,
    kb_service,
    model_provider_service,
    run_service,
    tool_service,
    trace_service,
)

logger = get_logger(__name__)

WORKFLOW_PAGE_LIMIT = 200
"""`GET /workflows` 的返回上限（配置类资源，量级很小）。"""

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
"""进程内后台任务集合：持有 `asyncio.Task` 的强引用（否则会被 GC 回收并静默取消）。"""

ORPHAN_TIMEOUT_MULTIPLIER = 2
"""6.3 第 3 条：`started_at` 超过 `timeout × 2` 的 `running` 行才算孤儿（避免误伤慢 Run）。"""

SHUTDOWN_GRACE_SECONDS = 5.0
"""进程收尾时等待后台 Run 任务收敛终态的上限（秒）。

`main.py` 的 lifespan 在 `dispose_engine()` 之前取消这些任务并等它们写完终态；
超时未收敛的行由下次启动的 `converge_orphan_runs` 兜底（6.3 第 3 条）。"""

RESUMABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        str(ErrorCode.MODEL_RATE_LIMITED),
        str(ErrorCode.MODEL_TIMEOUT),
        str(ErrorCode.TOOL_TIMEOUT),
    }
)
"""4.5.4 场景 1：`status=failed` 且失败原因属于**可重试类**。

4.5.4 列的第四个 `KB_EMPTY_INDEX` 属 Phase 5 的错误码，届时加入本集合即可。
"""


# --------------------------------------------------------------------------------------
# CRUD 与图校验（3.2.7）
# --------------------------------------------------------------------------------------
async def list_workflows(
    session: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    limit: int = WORKFLOW_PAGE_LIMIT,
) -> Sequence[Workflow]:
    """列表（3.2.7 的 `?status=`；附带 `?q=` 按名称模糊匹配）。"""
    filters = []
    if status:
        filters.append(Workflow.status == status)
    if q:
        filters.append(Workflow.name.contains(q))
    statement = select(Workflow).where(*filters).order_by(Workflow.updated_at.desc()).limit(max(1, limit))
    return (await session.execute(statement)).scalars().all()


async def get_workflow(session: AsyncSession, workflow_id: str) -> Workflow:
    """取 Workflow；不存在 → `WORKFLOW_NOT_FOUND`（404，附录 A）。"""
    row = await session.get(Workflow, workflow_id)
    if row is None:
        raise WorkflowNotFoundError(f"Workflow '{workflow_id}' does not exist", details={"workflow_id": workflow_id})
    return row


async def build_references(session: AsyncSession) -> GraphReferences:
    """装配图校验所需的已知集合：**enabled** 的 Agent（含其 `workflow_id`）与工具名（4.5.1）。"""
    agents = (
        (await session.execute(select(Agent).where(Agent.status == AGENT_STATUS_ENABLED, Agent.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    tools = (await session.execute(select(Tool).where(Tool.status == TOOL_STATUS_ENABLED))).scalars().all()
    return GraphReferences(
        agent_ids=[row.id for row in agents],
        tool_names=[row.name for row in tools],
        agent_workflow_ids={row.id: row.workflow_id for row in agents},
    )


async def validate_definition(
    session: AsyncSession,
    definition: Mapping[str, Any] | None,
    *,
    references: GraphReferences | None = None,
) -> tuple[list[dict[str, Any]], WorkflowGraph | None]:
    """只校验（`POST /workflows/{id}/validate`，3.2.7）：返回 `(errors, graph)`，**不抛异常**。

    `graph` 只在完全合法时非空 —— 它是解析后的只读投影（前端画简图用）。
    """
    refs = references if references is not None else await build_references(session)
    issues = graph_module.validate_definition(definition, references=refs)
    if issues:
        return [issue.as_dict() for issue in issues], None
    return [], parse_definition(definition, references=refs)


async def parse_workflow_definition(session: AsyncSession, definition: Mapping[str, Any]) -> WorkflowGraph:
    """写入路径共用：合法则返回图，否则 `WORKFLOW_INVALID_GRAPH`（422，`details.errors`）。"""
    errors, graph = await validate_definition(session, definition)
    if graph is None:
        raise WorkflowInvalidGraphError(
            "Workflow definition is not a valid graph", details={"errors": errors, "workflow": "definition"}
        )
    return graph


async def create_workflow(session: AsyncSession, data: WorkflowCreate) -> Workflow:
    """`POST /workflows`（3.2.7：`definition` 走图校验）。"""
    await _ensure_name_available(session, data.name)
    graph = await parse_workflow_definition(session, data.definition)
    row = Workflow(
        name=data.name,
        description=data.description,
        version=1,
        status=data.status,
        definition=dict(data.definition),
        state_schema=dict(data.state_schema),
        start_node_id=graph.start_node_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_workflow(session: AsyncSession, workflow_id: str, data: WorkflowUpdate) -> Workflow:
    """`PATCH /workflows/{id}`：定义变更后 `status` 回 `draft`（2.9）。"""
    row = await get_workflow(session, workflow_id)
    changes = data.model_dump(exclude_unset=True)
    name = changes.get("name")
    if name is not None and name != row.name:
        await _ensure_name_available(session, str(name))
    if "definition" in changes:
        graph = await parse_workflow_definition(session, changes.pop("definition"))
        row.definition = dict(graph.definition)
        row.start_node_id = graph.start_node_id
        if row.status != str(WorkflowStatus.DRAFT):
            row.status = str(WorkflowStatus.DRAFT)
            changes.setdefault("status", str(WorkflowStatus.DRAFT))
    if "state_schema" in changes:
        row.state_schema = dict(changes.pop("state_schema"))
    for key, value in changes.items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


async def publish_workflow(session: AsyncSession, workflow_id: str) -> Workflow:
    """`POST /workflows/{id}/publish`：`draft → published`，`version += 1`（3.2.7）。"""
    row = await get_workflow(session, workflow_id)
    await parse_workflow_definition(session, row.definition)
    row.status = str(WorkflowStatus.PUBLISHED)
    row.version = int(row.version) + 1
    await session.commit()
    await session.refresh(row)
    return row


async def delete_workflow(session: AsyncSession, workflow_id: str) -> None:
    """`DELETE /workflows/{id}`：解开 Agent 引用与历史运行，再删定义。

    SQLite 默认不开外键，因此这里**显式**清理（与 `tool_service.delete_tool` 同一写法，2.5）：

    - 仍有 `running` 的运行时拒绝（409，`WORKFLOW_HAS_RUNNING_RUNS`）；
    - `agents.workflow_id → NULL`（4.4.4：Agent 回落为"直接跑 AgentRuntime"）；
    - `runs.workflow_run_id → NULL`（2.6：观测行不因清理 Workflow 而消失）；
    - `node_runs` → `workflow_runs` → `workflows` 依次删除。
    """
    row = await get_workflow(session, workflow_id)
    running = (
        await session.execute(
            select(func.count(WorkflowRun.id)).where(
                WorkflowRun.workflow_id == workflow_id, WorkflowRun.status == str(RunStatus.RUNNING)
            )
        )
    ).scalar_one()
    if running:
        raise ConflictError(
            f"Workflow '{row.name}' still has {int(running)} running run(s)",
            details={"workflow_id": workflow_id, "running_runs": int(running), "reason": "WORKFLOW_HAS_RUNNING_RUNS"},
        )
    run_ids = select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow_id)
    await session.execute(update(Run).where(Run.workflow_run_id.in_(run_ids)).values(workflow_run_id=None))
    await session.execute(update(Agent).where(Agent.workflow_id == workflow_id).values(workflow_id=None))
    await session.execute(delete(NodeRun).where(NodeRun.run_id.in_(run_ids)))
    await session.execute(delete(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id))
    await session.delete(row)
    await session.commit()


async def _ensure_name_available(session: AsyncSession, name: str) -> None:
    """`workflows.name` 唯一（2.9）→ 冲突给 409。"""
    exists = (await session.execute(select(Workflow.id).where(Workflow.name == name))).first()
    if exists:
        raise ConflictError(f"Workflow name '{name}' already exists", details={"name": name})


# --------------------------------------------------------------------------------------
# 运行编排（3.2.7 / 4.5.3 / 4.5.4）
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class WorkflowRunHandle:
    """一次 Workflow 运行的句柄。

    `run_id` 是 `workflow_runs.id`，`api_run_id` 是 `runs.id`（4.8.1 的 trace 根：
    两者是一条 `runs` 记录 —— 见附录 F v1.13 对 `node_runs.agent_run_id` 的口径说明）；
    `queue` 仅在 Chat 内联模式非空（3.4：Workflow 只有在 Chat 里触发时才走 SSE）。
    """

    run_id: str
    api_run_id: str
    trace_id: str
    cancel_event: asyncio.Event
    task: asyncio.Task[None]
    queue: asyncio.Queue[SseQueueItem] | None = None


async def start_run(
    session: AsyncSession,
    settings: Settings,
    *,
    workflow: Workflow,
    payload: Mapping[str, Any] | None = None,
    trigger: str = TRIGGER_MANUAL,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    emit_queue: asyncio.Queue[SseQueueItem] | None = None,
    registry: run_service.RunRegistry | None = None,
    run_input: Mapping[str, Any] | None = None,
    begin_registry: bool = True,
) -> WorkflowRunHandle:
    """落库 + 起后台任务（3.2.7：`POST /workflows/{id}/runs` 返回 202）。

    - 运行前**再校验一次** `definition`（双保险：图是快照，但引用可能已变化）；
    - `runs` 行与 `workflow_runs` 行一起建：前者是 4.8.1 的 trace 根，后者是业务实例；
    - `registry.begin()` 让 `POST /runs/{id}/cancel` 也能取消 Workflow 运行；
    - Chat 内联模式由 `chat_service` 传入 `conversation_id` / `emit_queue`（3.4 事件 13/14）。
    """
    if str(workflow.status) == str(WorkflowStatus.ARCHIVED):
        raise ConflictError(
            f"Workflow '{workflow.name}' is archived and cannot be started",
            details={"workflow_id": workflow.id, "status": workflow.status, "reason": "WORKFLOW_ARCHIVED"},
        )
    graph = await parse_workflow_definition(session, workflow.definition)
    run_id = ids.new_ulid()
    api_run_id = ids.new_ulid()
    trace_id = ids.new_trace_id()
    initial = dict(payload or {})

    workflow_run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        workflow_version=int(workflow.version),
        definition_snapshot=dict(workflow.definition),
        status=str(RunStatus.RUNNING),
        trigger=trigger,
        input=_jsonable(initial),
        output={},
        state=_jsonable(_initial_state_value(initial, run_id=api_run_id, workflow_id=workflow.id)),
        current_node_id=graph.start_node_id,
        checkpoint={},
        trace_id=trace_id,
        started_at=_utcnow(),
    )
    session.add(workflow_run)
    api_run = Run(
        id=api_run_id,
        kind=str(RunKind.WORKFLOW),
        agent_id=agent_id,
        conversation_id=conversation_id,
        workflow_run_id=run_id,
        status=str(RunStatus.RUNNING),
        input=_jsonable(dict(run_input or initial)),
        output={},
        trace_id=trace_id,
        started_at=_utcnow(),
    )
    session.add(api_run)
    await session.commit()
    await session.refresh(workflow_run)

    run_registry = registry or run_service.get_run_registry(settings)
    cancel_event = (
        run_registry.begin(run_id=api_run_id, conversation_id=conversation_id) if begin_registry else asyncio.Event()
    )
    task = asyncio.create_task(
        _execute(
            settings=settings,
            workflow_run_id=run_id,
            api_run_id=api_run_id,
            trace_id=trace_id,
            workflow_name=str(workflow.name),
            definition_snapshot=dict(workflow.definition),
            initial_state=_initial_state_value(initial, run_id=api_run_id, workflow_id=workflow.id),
            trigger=trigger,
            conversation_id=conversation_id,
            emit_queue=emit_queue,
            cancel_event=cancel_event,
            registry=run_registry if begin_registry else None,
            resume_checkpoint=None,
        ),
        name=f"workflow-run:{run_id}",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return WorkflowRunHandle(
        run_id=run_id,
        api_run_id=api_run_id,
        trace_id=trace_id,
        cancel_event=cancel_event,
        task=task,
        queue=emit_queue,
    )


async def resume_run(
    session: AsyncSession,
    settings: Settings,
    run_id: str,
    *,
    emit_queue: asyncio.Queue[SseQueueItem] | None = None,
    registry: run_service.RunRegistry | None = None,
) -> WorkflowRun:
    """`POST /workflow-runs/{run_id}/resume`（4.5.4 的**两类**合法场景，其余 409）。

    1. `status=failed` 且 `error_code ∈ RESUMABLE_ERROR_CODES`；
    2. `status=running` 但**不在本进程的活跃表**里（进程重启后的孤儿 Run）且有 `current_node_id`。

    每次续跑都是一次**新的 `runs` 行 + 新的 trace**（4.8.1 的 trace 根一次执行一个）：
    `workflow_runs.trace_id` 指向最后一次尝试，历史尝试仍可在 Trace 列表里查到（附录 F v1.13）。
    """
    workflow_run = await get_run(session, run_id)
    workflow = await get_workflow(session, workflow_run.workflow_id)
    run_registry = registry or run_service.get_run_registry(settings)

    status = str(workflow_run.status)
    if status in {str(RunStatus.SUCCEEDED), str(RunStatus.CANCELED), str(RunStatus.PENDING)}:
        raise WorkflowRunNotResumableError(
            f"Workflow run '{run_id}' is {status} and cannot be resumed",
            details={"run_id": run_id, "status": status, "reason": "RUN_ALREADY_FINISHED"},
        )
    if status == str(RunStatus.FAILED) and str(workflow_run.error_code or "") not in RESUMABLE_ERROR_CODES:
        raise WorkflowRunNotResumableError(
            f"Workflow run '{run_id}' failed with a non-retryable error",
            details={
                "run_id": run_id,
                "status": status,
                "error_code": workflow_run.error_code,
                "resumable_error_codes": sorted(RESUMABLE_ERROR_CODES),
                "reason": "ERROR_NOT_RETRYABLE",
            },
        )
    if status == str(RunStatus.RUNNING):
        previous_api_run_id = await _active_api_run_id(session, workflow_run.id)
        if previous_api_run_id and run_registry.is_active(previous_api_run_id):
            raise WorkflowRunNotResumableError(
                f"Workflow run '{run_id}' is still running in this process",
                details={"run_id": run_id, "status": status, "reason": "RUN_STILL_ACTIVE"},
            )
        if not workflow_run.current_node_id:
            raise WorkflowRunNotResumableError(
                f"Workflow run '{run_id}' has no checkpoint to resume from",
                details={"run_id": run_id, "status": status, "reason": "NO_CHECKPOINT"},
            )

    api_run_id = ids.new_ulid()
    trace_id = ids.new_trace_id()
    workflow_run.status = str(RunStatus.RUNNING)
    workflow_run.trace_id = trace_id
    workflow_run.ended_at = None
    workflow_run.latency_ms = None
    workflow_run.error_code = None
    workflow_run.error_message = None
    session.add(
        Run(
            id=api_run_id,
            kind=str(RunKind.WORKFLOW),
            agent_id=None,
            conversation_id=None,
            workflow_run_id=workflow_run.id,
            status=str(RunStatus.RUNNING),
            input=_jsonable({"resume_from": workflow_run.current_node_id}),
            output={},
            trace_id=trace_id,
            started_at=_utcnow(),
        )
    )
    await session.commit()
    await session.refresh(workflow_run)

    cancel_event = run_registry.begin(run_id=api_run_id, conversation_id=None)
    task = asyncio.create_task(
        _execute(
            settings=settings,
            workflow_run_id=workflow_run.id,
            api_run_id=api_run_id,
            trace_id=trace_id,
            workflow_name=str(workflow.name),
            definition_snapshot=dict(workflow_run.definition_snapshot),
            initial_state=dict(workflow_run.state or {}),
            trigger=str(workflow_run.trigger),
            conversation_id=None,
            emit_queue=emit_queue,
            cancel_event=cancel_event,
            registry=run_registry,
            resume_checkpoint=dict(workflow_run.checkpoint or {}),
        ),
        name=f"workflow-resume:{workflow_run.id}",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return workflow_run


async def cancel_run(
    session: AsyncSession,
    run_id: str,
    *,
    settings: Settings,
    registry: run_service.RunRegistry | None = None,
) -> WorkflowRun:
    """`POST /workflow-runs/{run_id}/cancel`（3.2.7）：活跃 → 置取消信号；否则直接落终态。"""
    workflow_run = await get_run(session, run_id)
    if str(workflow_run.status) in run_service.TERMINAL_STATUSES:
        raise RunAlreadyFinishedError(
            f"Workflow run '{run_id}' is already {workflow_run.status}",
            details={"run_id": run_id, "status": workflow_run.status},
        )
    run_registry = registry or run_service.get_run_registry(settings)
    api_run_id = await _active_api_run_id(session, workflow_run.id)
    active = bool(api_run_id)
    if active and api_run_id is not None:
        active = run_registry.request_cancel(api_run_id)
    if not active:
        now = _utcnow()
        workflow_run.status = str(RunStatus.CANCELED)
        workflow_run.error_code = str(ErrorCode.RUN_CANCELED)
        workflow_run.error_message = "Workflow run was canceled before it finished"
        workflow_run.ended_at = now
        workflow_run.latency_ms = int((now - workflow_run.started_at).total_seconds() * 1000)
        if api_run_id:
            api_run = await session.get(Run, api_run_id)
            if api_run is not None and str(api_run.status) not in run_service.TERMINAL_STATUSES:
                api_run.status = str(RunStatus.CANCELED)
                api_run.error_code = str(ErrorCode.RUN_CANCELED)
                api_run.error_message = "Workflow run was canceled before it finished"
                api_run.canceled_at = now
                api_run.ended_at = now
        await session.commit()
        await session.refresh(workflow_run)
    return workflow_run


async def get_run(session: AsyncSession, run_id: str) -> WorkflowRun:
    """取 WorkflowRun；不存在 → `WORKFLOW_RUN_NOT_FOUND`（404，附录 A）。"""
    row = await session.get(WorkflowRun, run_id)
    if row is None:
        raise WorkflowRunNotFoundError(f"Workflow run '{run_id}' does not exist", details={"run_id": run_id})
    return row


async def list_runs(
    session: AsyncSession,
    *,
    workflow_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[WorkflowRun], int]:
    """运行列表（3.2.7 的 `?workflow_id=&status=`）。"""
    filters = []
    if workflow_id:
        filters.append(WorkflowRun.workflow_id == workflow_id)
    if status:
        filters.append(WorkflowRun.status == status)
    total = (await session.execute(select(func.count(WorkflowRun.id)).where(*filters))).scalar_one()
    statement = (
        select(WorkflowRun)
        .where(*filters)
        .order_by(WorkflowRun.started_at.desc())
        .offset(max(0, page - 1) * page_size)
        .limit(page_size)
    )
    return (await session.execute(statement)).scalars().all(), int(total)


async def list_node_runs(session: AsyncSession, run_id: str) -> Sequence[NodeRun]:
    """节点执行记录（3.2.7：按 `seq` 排序；运行不存在 → 404）。"""
    await get_run(session, run_id)
    statement = select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.seq.asc())
    return (await session.execute(statement)).scalars().all()


async def converge_orphan_runs(session: AsyncSession, settings: Settings) -> dict[str, int]:
    """6.3 第 3 条：把"进程重启后仍 `running` 且超时"的 Run / WorkflowRun 标为 `failed`。

    - `workflow_runs`：超时阈值取 `definition_snapshot.config.timeout_seconds × 2`（2.6 / 6.3）；
    - `runs`：WorkflowRun 关联行用同一阈值；普通 Chat Run 用 `agents.timeout_seconds × 2`，
      Agent 已删则回落 `AGENT_RUN_TIMEOUT_SECONDS × 2`；
    - 幂等：只处理 `running` 行，标为 `failed` / `RUN_ABANDONED` 后不再被扫到。
    """
    now = _utcnow()
    counts = {"workflow_runs": 0, "runs": 0}

    workflow_rows = (
        (await session.execute(select(WorkflowRun).where(WorkflowRun.status == str(RunStatus.RUNNING)))).scalars().all()
    )
    workflow_timeouts: dict[str, float] = {}
    abandoned_ids: list[str] = []
    for row in workflow_rows:
        timeout = _workflow_timeout_seconds(row.definition_snapshot)
        workflow_timeouts[row.id] = timeout
        if _elapsed_seconds(row.started_at, now) <= timeout * ORPHAN_TIMEOUT_MULTIPLIER:
            continue
        row.status = str(RunStatus.FAILED)
        row.error_code = str(ErrorCode.RUN_ABANDONED)
        row.error_message = "Workflow run was abandoned after a process restart"
        row.ended_at = now
        row.latency_ms = int(_elapsed_seconds(row.started_at, now) * 1000)
        counts["workflow_runs"] += 1
        abandoned_ids.append(row.id)

    for abandoned_id in abandoned_ids:
        # 节点行也要收尾：否则详情页的节点表会一直显示 `running`（4.5.3 的状态语义）
        await _converge_running_node_runs(
            session,
            abandoned_id,
            now=now,
            status=RunStatus.FAILED,
            error_code=str(ErrorCode.RUN_ABANDONED),
            error_message="Workflow node was abandoned after a process restart",
        )

    runs = (await session.execute(select(Run).where(Run.status == str(RunStatus.RUNNING)))).scalars().all()
    agent_ids = {row.agent_id for row in runs if row.agent_id}
    agents = (
        {
            item.id: item
            for item in (await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()
        }
        if agent_ids
        else {}
    )
    for run_row in runs:
        if run_row.workflow_run_id and run_row.workflow_run_id in workflow_timeouts:
            timeout = workflow_timeouts[run_row.workflow_run_id]
        else:
            agent = agents.get(run_row.agent_id or "")
            timeout = float(agent.timeout_seconds) if agent is not None else float(settings.agent_run_timeout_seconds)
        if _elapsed_seconds(run_row.started_at, now) <= timeout * ORPHAN_TIMEOUT_MULTIPLIER:
            continue
        run_row.status = str(RunStatus.FAILED)
        run_row.error_code = str(ErrorCode.RUN_ABANDONED)
        run_row.error_message = "Run was abandoned after a process restart"
        run_row.ended_at = now
        run_row.latency_ms = int(_elapsed_seconds(run_row.started_at, now) * 1000)
        counts["runs"] += 1

    if counts["workflow_runs"] or counts["runs"]:
        await session.commit()
        logger.info("workflow.orphan_runs_converged", **counts)
    return counts


# --------------------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------------------
def _utcnow() -> datetime:
    """naive UTC（与 `db/base.now_utc` 一致，0.2.1）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _naive(value: datetime) -> datetime:
    """aware → naive UTC（SQLite 存 naive）。"""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _elapsed_seconds(started_at: datetime, now: datetime) -> float:
    return (now - _naive(started_at)).total_seconds()


def _jsonable(value: Any) -> Any:
    """JSON 列写入前的转换（`_Undefined` / `Decimal` / `datetime` 都要过一遍）。"""
    return template_module.to_jsonable(value)


def _initial_state_value(payload: Mapping[str, Any], *, run_id: str, workflow_id: str) -> dict[str, Any]:
    """初始 state：业务字段 + `run` 命名空间（4.5.2）。"""
    return state_module.initial_state(payload, run={"run_id": run_id, "workflow_id": workflow_id})


def _workflow_timeout_seconds(definition: Mapping[str, Any] | None) -> float:
    return float(definition_config(dict(definition or {})).get("timeout_seconds", 600))


async def _active_api_run_id(session: AsyncSession, workflow_run_id: str) -> str | None:
    """取该 WorkflowRun **最近一次**执行的 `runs.id`（cancel / resume 用它查活跃表）。"""
    statement = select(Run.id).where(Run.workflow_run_id == workflow_run_id).order_by(Run.started_at.desc()).limit(1)
    return (await session.execute(statement)).scalars().first()


# --------------------------------------------------------------------------------------
# 落库 sink 与节点执行器（4.5.3 / 4.5.2）
# --------------------------------------------------------------------------------------
class DatabaseWorkflowRunSink:
    """`WorkflowRunSink` 的 ORM 实现（4.5.3）。

    - `node_started`：插入一行 `node_runs`（`status=running`）并**立即 commit**（进节点即落库）；
    - `node_finished`：更新该行为终态（状态 / 输出摘要 / 耗时 / 错误 / `agent_run_id`）；
    - `state_updated`：更新 `workflow_runs.state` / `current_node_id` / `checkpoint`
      —— 4.5.3 要求"每个节点结束时更新"，前端即使只轮询也能看到进度。
    """

    def __init__(self, session: AsyncSession, workflow_run: WorkflowRun) -> None:
        self._session = session
        self._run = workflow_run
        self._seq_offset: int | None = None
        """本次尝试的 `seq` 偏移：`resume` 后同一 WorkflowRun 里已有行，续跑行必须接在后面。

        3.2.7 明确"按 `seq` 排序"，而引擎的计数器每次尝试都从 1 开始 —— 若直接落库，
        attempt 1 与 attempt 2 的行会交错，前端排序不稳定。这里用 `MAX(seq)` 做偏移。
        """

    async def node_started(self, record: NodeRunStarted) -> str:
        if self._seq_offset is None:
            current_max = (
                await self._session.execute(select(func.max(NodeRun.seq)).where(NodeRun.run_id == self._run.id))
            ).scalar()
            self._seq_offset = int(current_max or 0)
        row = NodeRun(
            run_id=self._run.id,
            node_id=record.node_id,
            node_type=record.node_type,
            name=record.name,
            seq=self._seq_offset + record.seq,
            iteration=record.iteration,
            attempt=record.attempt,
            status=str(RunStatus.RUNNING),
            input=_jsonable(record.input),
            output={},
            trace_id=self._run.trace_id,
            started_at=_utcnow(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row.id

    async def node_finished(self, record: NodeRunFinished) -> None:
        row = await self._session.get(NodeRun, record.node_run_id)
        if row is None:  # pragma: no cover - 只可能出现在"行被显式删除"的场景
            logger.warning("workflow.node_run_missing", node_run_id=record.node_run_id)
            return
        row.status = str(record.status)
        row.output = _jsonable(record.output)
        row.agent_run_id = record.agent_run_id
        row.span_id = record.span_id
        row.error_code = record.error_code
        row.error_message = record.error_message
        row.latency_ms = record.latency_ms
        row.ended_at = _utcnow()
        await self._session.commit()

    async def state_updated(
        self, *, state: Mapping[str, Any], current_node_id: str | None, checkpoint: Mapping[str, Any]
    ) -> None:
        self._run.state = _jsonable(dict(state))
        self._run.current_node_id = current_node_id
        self._run.checkpoint = _jsonable(dict(checkpoint))
        await self._session.commit()


@dataclass(slots=True)
class NodeRunnerUsage:
    """节点执行累计的用量与调用数（写回 `runs` 行，4.8.3）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    tool_call_count: int = 0


class ServiceNodeRunner:
    """`WorkflowNodeRunner`：把三类"有 IO"的节点接到既有实现上（7.4 任务 2 / 4.5.2）。

    - `agent`：复用 `AgentRuntime.run()`（4.4.4）。Chat 内联场景共用会话（消息落 `messages`、
      可被后续节点看到）；手动运行场景用进程内短期记忆（节点产物只进 state / `node_runs`）；
    - `tool`：复用九步流水线（`ToolExecutor`）与同一套 `tool_invocations` 审计（4.2.3）；
    - `retriever`：Phase 3 **明确不可用**（KB 属 Phase 5）→ `NOT_IMPLEMENTED`；
      节点默认 `on_error=continue` 会把错误文本写进 `output_key`（4.5.2），不做"假的可用性"。
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        api_run_id: str,
        conversation_id: str | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._api_run_id = api_run_id
        self._conversation_id = conversation_id
        self.usage = NodeRunnerUsage()
        self._executor = ToolExecutor(
            registry=default_registry(),
            settings=settings,
            sink=tool_service.DatabaseToolInvocationSink(session),
        )

    async def run_agent_node(self, node: WorkflowNode, input_text: str, ctx: NodeExecutionContext) -> NodeOutcome:
        agent = await agent_service.get_agent(self._session, node.str_field("agent_id"))
        if agent.status != AGENT_STATUS_ENABLED:
            raise WorkflowNodeError(
                ErrorCode.AGENT_DISABLED,
                f"Agent '{agent.name}' is disabled",
                details={"node_id": node.id, "agent_id": agent.id},
            )
        provider = await model_provider_service.get_provider(self._session, agent.model_provider_id)
        spec = agent_service.to_spec(agent)
        conversation_id = self._conversation_id or f"wfnode-{self._api_run_id}-{node.id}-{ctx.iteration}"
        memory: ShortTermMemory = (
            conversation_service.SqlShortTermMemory(self._session)
            if self._conversation_id
            else InMemoryShortTermMemory()
        )
        runtime = AgentRuntime(
            llm=LLMRegistry(self._settings).create(model_provider_service.to_config(provider, settings=self._settings)),
            memory=memory,
            tracer=ctx.tracer,
            settings=self._settings,
            toolkit=tool_service.build_toolkit(self._session, self._settings, tool_ids=spec.tool_ids),
        )
        result = await runtime.run(
            spec, input_text, conversation_id=conversation_id, emit=ctx.emitter, cancel=ctx.cancel
        )
        self.usage.prompt_tokens += result.usage.prompt_tokens
        self.usage.completion_tokens += result.usage.completion_tokens
        self.usage.cost_usd += result.cost_usd
        self.usage.tool_call_count += result.tool_call_count
        if result.status is not RunStatus.SUCCEEDED:
            raise WorkflowNodeError(
                _error_code(result.error_code),
                result.error_message or f"Agent node '{node.id}' failed",
                details={"node_id": node.id, "agent_id": agent.id, "agent_run_id": self._api_run_id},
            )
        return NodeOutcome(
            state_updates={"output": result.output_text},
            output_summary={
                "agent_id": agent.id,
                "agent_name": agent.name,
                "steps": result.steps,
                "tool_call_count": result.tool_call_count,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "cost_usd": float(result.cost_usd),
            },
            agent_run_id=self._api_run_id,
        )

    async def run_tool_node(
        self, node: WorkflowNode, arguments: Mapping[str, Any], ctx: NodeExecutionContext
    ) -> NodeOutcome:
        name = node.str_field("tool_name")
        row = (await self._session.execute(select(Tool).where(Tool.name == name))).scalars().first()
        if row is None:
            raise WorkflowNodeError(
                ErrorCode.TOOL_NOT_FOUND,
                f"Tool '{name}' does not exist",
                details={"node_id": node.id, "tool_name": name},
            )
        if row.status != TOOL_STATUS_ENABLED:
            raise WorkflowNodeError(
                ErrorCode.TOOL_DISABLED,
                f"Tool '{name}' is disabled",
                details={"node_id": node.id, "tool_name": name, "status": row.status},
            )
        definition = tool_service.to_definition(row)
        result = await self._executor.execute(
            ToolCallSpec(id=f"{node.id}-{ctx.iteration}", name=name, arguments=dict(arguments)),
            ctx=ToolRunContext(
                run_id=self._api_run_id,
                definitions={name: definition},
                settings=self._settings,
                tracer=ctx.tracer,
                emitter=ctx.emitter,
                cancellation=ctx.cancel,
                sandbox_root=self._settings.sandbox_path,
            ),
        )
        self.usage.tool_call_count += 1
        if not result.succeeded:
            raise WorkflowNodeError(
                _error_code(result.error_code),
                result.error_message or f"Tool '{name}' failed",
                details={
                    "node_id": node.id,
                    "tool_name": name,
                    "tool_call_id": result.tool_call_id,
                    "reason": result.meta.get("reason"),
                },
            )
        return NodeOutcome(
            state_updates={"output": result.content},
            output_summary={
                "tool_name": name,
                "tool_call_id": result.tool_call_id,
                "latency_ms": result.latency_ms,
                "truncated": result.truncated,
            },
        )

    async def run_retriever_node(self, node: WorkflowNode, query: str, ctx: NodeExecutionContext) -> NodeOutcome:
        """`retriever` 节点：`kb_id` → `kb_service.retrieve()`（4.5.2 / 4.6.3，Phase 5 接入）。

        - 命中切片以 **JSON 形态**写进 `output_key`（`{{state.<key>}}` 可直接渲染、`len()` 可判空），
          每片带 `source`（4.6.3 的引用串 `install.md#2.1 page=2 score=0.83`）供下游 `agent` 节点引用；
        - 空查询 / 空索引 → `hit_count=0`，**不算节点失败**：要不要答、怎么答交给图的
          `condition` 分支（`configs/workflows/kb-qa-flow.yaml` 的"未命中必须说不编造"就是这条）；
        - KB 不存在 / 已软删 → `kb_service` 的 `KB_NOT_FOUND` 冒泡，引擎记 `node_runs.error_code`
          后按节点 `on_error` 处理（`retriever` 默认 `continue`，4.5.2）；
        - 命中后发事件 10（3.4）：Chat 内联场景前端据此渲染引用来源；手动运行注入的是
          `NullEmitter`，无副作用。
        """
        kb_id = node.str_field("kb_id")
        chunks, used_kb_ids = await kb_service.retrieve(self._session, [kb_id], query, settings=self._settings)
        payload = [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "kb_id": chunk.kb_id,
                "score": round(chunk.score, 6),
                "content": chunk.content,
                "source": chunk.citation_source(),
            }
            for chunk in chunks
        ]
        await ctx.emitter.emit(
            SseEventType.RETRIEVAL_COMPLETED,
            RetrievalCompletedPayload(kb_ids=used_kb_ids, query=query, hit_count=len(payload)),
        )
        return NodeOutcome(
            state_updates={"output": payload},
            output_summary={
                "kb_id": kb_id,
                "used_kb_ids": used_kb_ids,
                "hit_count": len(payload),
                "sources": [str(item["source"]) for item in payload],
            },
        )


def _error_code(value: str | None) -> ErrorCode:
    """错误码字符串 → `ErrorCode`（未知值回落 `INTERNAL_ERROR`，不让字符串漏进节点结果）。"""
    if not value:
        return ErrorCode.INTERNAL_ERROR
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.INTERNAL_ERROR


# --------------------------------------------------------------------------------------
# 执行阶段（后台任务）
# --------------------------------------------------------------------------------------
async def _execute(
    *,
    settings: Settings,
    workflow_run_id: str,
    api_run_id: str,
    trace_id: str,
    workflow_name: str,
    definition_snapshot: Mapping[str, Any],
    initial_state: Mapping[str, Any],
    trigger: str,
    conversation_id: str | None,
    emit_queue: asyncio.Queue[SseQueueItem] | None,
    cancel_event: asyncio.Event,
    registry: run_service.RunRegistry | None,
    resume_checkpoint: Mapping[str, Any] | None,
) -> None:
    """执行阶段：独立 session + `Tracer` 落库 + 引擎推进，异常一律收敛为终态。

    与 `chat_service._execute` 同一套结构（7.2 任务 6）：客户端断开不影响 Run（3.4），
    SSE（`emit_queue` 非空时）只在内联模式下有消费者。
    """
    emitter: EventEmitter = QueueEmitter(emit_queue) if emit_queue is not None else NullEmitter()
    canceled_exc: asyncio.CancelledError | None = None
    """被取消时保存的取消异常（收尾后重新抛出，保留任务的取消语义，见 W2）。"""
    finalized = False
    """是否已在 session 内收敛过终态（外层兜底据此避免重复写入）。"""
    try:
        if registry is not None:
            await registry.acquire()
        async with get_sessionmaker()() as session:
            sink = trace_service.DatabaseSpanSink(session)
            tracer = Tracer.from_settings(settings, sink=sink)
            run_trace: RunTrace | None = None
            result: WorkflowResult | None = None
            runner: ServiceNodeRunner | None = None
            status = RunStatus.SUCCEEDED
            error: AppError | None = None
            try:
                graph = await parse_workflow_definition(session, definition_snapshot)
                run_trace = tracer.start_run(
                    kind=RunKind.WORKFLOW,
                    name=f"workflow:{workflow_name}",
                    run_id=api_run_id,
                    trace_id=trace_id,
                    attributes={
                        "workflow_run_id": workflow_run_id,
                        "workflow_name": workflow_name,
                        "trigger": trigger,
                        "conversation_id": conversation_id,
                    },
                )
                await sink.open_trace(run_trace)
                if emit_queue is not None:
                    await emitter.emit(
                        SseEventType.RUN_STARTED,
                        RunStartedPayload(
                            run_id=api_run_id,
                            trace_id=trace_id,
                            conversation_id=conversation_id,
                            started_at=_utcnow(),
                        ),
                    )
                workflow_run = await get_run(session, workflow_run_id)
                workflow_sink = DatabaseWorkflowRunSink(session, workflow_run)
                runner = ServiceNodeRunner(
                    session=session,
                    settings=settings,
                    api_run_id=api_run_id,
                    conversation_id=conversation_id,
                )
                engine = SimpleEngine()
                timeout = _workflow_timeout_seconds(definition_snapshot)
                if resume_checkpoint is None:
                    state = state_module.initial_state(
                        initial_state, run={"run_id": api_run_id, "workflow_id": workflow_run.workflow_id}
                    )
                    result = await asyncio.wait_for(
                        engine.run(
                            graph,
                            state,
                            run_id=api_run_id,
                            emit=emitter,
                            cancel=cancel_event,
                            tracer=tracer,
                            sink=workflow_sink,
                            runner=runner,
                        ),
                        timeout=timeout,
                    )
                else:
                    result = await asyncio.wait_for(
                        engine.resume(
                            resume_checkpoint,
                            graph=graph,
                            run_id=api_run_id,
                            emit=emitter,
                            cancel=cancel_event,
                            tracer=tracer,
                            sink=workflow_sink,
                            runner=runner,
                        ),
                        timeout=timeout,
                    )
                status = result.status
                error = None if result.error_code is None else _workflow_error(result)
            except TimeoutError:
                status = RunStatus.FAILED
                error = _timeout_error()
            except AppError as exc:
                status = RunStatus.CANCELED if str(exc.code) == str(ErrorCode.RUN_CANCELED) else RunStatus.FAILED
                error = exc
            except asyncio.CancelledError as exc:
                # 进程收尾（lifespan 的 `shutdown_active_runs`）或外部取消：**仍要落终态**，
                # 否则该行永远停在 `running`（W2）。收敛完再把取消抛回去，保留取消语义。
                canceled_exc = exc
                status = RunStatus.CANCELED
                error = RunCanceledError("Workflow run was canceled")
            except Exception:
                logger.error("workflow.run_unexpected_error", run_id=workflow_run_id, exc_info=True)
                status = RunStatus.FAILED
                error = InternalError("Unexpected error while running the workflow")
            finalized = await _finalize_resilient(
                session=session,
                workflow_run_id=workflow_run_id,
                api_run_id=api_run_id,
                status=status,
                result=result,
                error=error,
                runner=runner,
                run_trace=run_trace,
                tracer=tracer,
                span_sink=sink,
                emit=emitter if emit_queue is not None else None,
            )
            logger.info(
                "workflow.run_finished",
                workflow_run_id=workflow_run_id,
                api_run_id=api_run_id,
                trace_id=trace_id,
                status=str(status),
                error_code=str(error.code) if error is not None else None,
            )
            if canceled_exc is not None:
                raise canceled_exc
    except asyncio.CancelledError:
        # 取消发生在**排队等并发闸门**阶段（还没进 session）：没有 session 可收敛，走独立 session 兜底
        if not finalized:
            await _converge_canceled_run(workflow_run_id, api_run_id, reason="Workflow run was canceled")
        raise
    finally:
        if emit_queue is not None:
            await emit_queue.put(None)
        if registry is not None:
            registry.finish(api_run_id)
            registry.release()


async def _finalize(
    *,
    session: AsyncSession,
    workflow_run_id: str,
    api_run_id: str,
    status: RunStatus,
    result: WorkflowResult | None,
    error: AppError | None,
    runner: ServiceNodeRunner | None,
    run_trace: RunTrace | None,
    tracer: Tracer,
    span_sink: trace_service.DatabaseSpanSink,
    emit: EventEmitter | None,
) -> None:
    """收敛终态：`workflow_runs` + `runs` + `traces` + 结果事件（一次收尾，成功失败共用）。"""
    now = _utcnow()
    error_code = str(error.code) if error is not None else (result.error_code if result else None)
    error_message = error.message if error is not None else (result.error_message if result else None)

    workflow_run = await session.get(WorkflowRun, workflow_run_id)
    if workflow_run is not None:
        workflow_run.status = str(status)
        workflow_run.ended_at = now
        workflow_run.latency_ms = int(_elapsed_seconds(workflow_run.started_at, now) * 1000)
        workflow_run.error_code = error_code
        workflow_run.error_message = error_message
        if result is not None:
            workflow_run.output = _jsonable(result.output)
            workflow_run.state = _jsonable(result.state)
            if result.current_node_id is not None:
                workflow_run.current_node_id = result.current_node_id

    abandoned = await _converge_running_node_runs(
        session,
        workflow_run_id,
        now=now,
        status=RunStatus.CANCELED if status is RunStatus.CANCELED else RunStatus.FAILED,
        error_code=error_code or str(ErrorCode.RUN_ABANDONED),
        error_message=error_message or "Workflow run finished before this node did",
    )
    if abandoned:
        # 引擎层的取消收尾（`_run_attempt` 的 `CancelledError` 分支）之外还有兜底：连接被摘掉 /
        # 进程被掐停的行也在这里收干净（W3）
        logger.warning("workflow.node_runs_converged", workflow_run_id=workflow_run_id, rows=abandoned)

    api_run = await session.get(Run, api_run_id)
    if api_run is not None:
        api_run.status = str(status)
        api_run.ended_at = now
        api_run.latency_ms = int(_elapsed_seconds(api_run.started_at, now) * 1000)
        api_run.error_code = error_code
        api_run.error_message = error_message
        if status is RunStatus.CANCELED:
            # 与 `run_service.cancel_run` / `finish_run` 的语义对齐（2.6）：取消时间必须可查
            api_run.canceled_at = now
        api_run.steps = result.steps if result is not None else 0
        if result is not None:
            api_run.output = _jsonable(result.output)
        if runner is not None:
            api_run.tool_call_count = runner.usage.tool_call_count
            api_run.prompt_tokens = runner.usage.prompt_tokens
            api_run.completion_tokens = runner.usage.completion_tokens
            api_run.total_tokens = runner.usage.prompt_tokens + runner.usage.completion_tokens
            api_run.total_cost_usd = runner.usage.cost_usd
    await session.commit()

    if run_trace is None:
        return
    await tracer.end_run(
        run_trace,
        status=SpanStatus.OK if status is RunStatus.SUCCEEDED else SpanStatus.ERROR,
        error_code=error_code,
        error_message=error_message,
    )
    # 观测是副产物（4.8.2）：`close_trace` 失败不能挡住会话事件通知与调用方的收尾逻辑
    try:
        await span_sink.close_trace(run_trace, status=status, error_code=error_code, error_message=error_message)
    except Exception:
        logger.warning("workflow.close_trace_failed", workflow_run_id=workflow_run_id, exc_info=True)

    if emit is None:
        return
    if status is RunStatus.SUCCEEDED and result is not None:
        await emit.emit(
            SseEventType.RUN_COMPLETED,
            RunCompletedPayload(
                run_id=api_run_id,
                status=str(status),
                steps=result.steps,
                tool_call_count=runner.usage.tool_call_count if runner is not None else 0,
                latency_ms=result.latency_ms,
            ),
        )
        return
    await emit.emit(
        SseEventType.RUN_FAILED,
        RunFailedPayload(
            run_id=api_run_id,
            error_code=error_code or str(ErrorCode.INTERNAL_ERROR),
            error_message=error_message or "",
        ),
    )


async def _finalize_resilient(
    *,
    session: AsyncSession,
    workflow_run_id: str,
    api_run_id: str,
    status: RunStatus,
    result: WorkflowResult | None,
    error: AppError | None,
    runner: ServiceNodeRunner | None,
    run_trace: RunTrace | None,
    tracer: Tracer,
    span_sink: trace_service.DatabaseSpanSink,
    emit: EventEmitter | None,
) -> bool:
    """收敛终态（抗"session 已失效"）：失败时换**独立 session** 再收敛一次（W1）。

    为什么需要：`asyncio.wait_for` 超时或任务取消会打断引擎里正在进行的 `commit()` ——
    SQLAlchemy 把 session 标记成"必须 rollback"，直接调 `_finalize` 会抛 `PendingRollbackError`
    穿透整条收尾路径，于是 `workflow_runs` / `runs` 永远停在 `running`、没有任何
    `workflow.run_finished` 日志（实测：超时 12s 后仍为 running，且排除了 `database is locked`）。

    返回是否成功落库：取消路径上据此决定要不要再兜底一次。
    """
    await _safe_rollback(session)
    try:
        await _finalize(
            session=session,
            workflow_run_id=workflow_run_id,
            api_run_id=api_run_id,
            status=status,
            result=result,
            error=error,
            runner=runner,
            run_trace=run_trace,
            tracer=tracer,
            span_sink=span_sink,
            emit=emit,
        )
        return True
    except Exception:
        logger.error(
            "workflow.finalize_failed",
            workflow_run_id=workflow_run_id,
            api_run_id=api_run_id,
            status=str(status),
            exc_info=True,
        )
    await _safe_rollback(session)
    try:
        async with get_sessionmaker()() as fresh_session:
            await _finalize(
                session=fresh_session,
                workflow_run_id=workflow_run_id,
                api_run_id=api_run_id,
                status=status,
                result=result,
                error=error,
                runner=runner,
                run_trace=run_trace,
                tracer=tracer,
                span_sink=trace_service.DatabaseSpanSink(fresh_session),
                emit=emit,
            )
        return True
    except Exception:
        logger.error(
            "workflow.finalize_retry_failed",
            workflow_run_id=workflow_run_id,
            api_run_id=api_run_id,
            status=str(status),
            exc_info=True,
        )
        return False


async def _safe_rollback(session: AsyncSession) -> None:
    """回滚（幂等）：干净的 session 上再 rollback 是无害的 no-op。"""
    try:
        await session.rollback()
    except Exception:  # pragma: no cover - 连接已断时 rollback 也可能失败
        logger.warning("workflow.rollback_failed", exc_info=True)


# ---- 独立 session 的收尾路径（W1 / W2 / W3 的兜底） ----
async def _converge_running_node_runs(
    session: AsyncSession,
    workflow_run_id: str,
    *,
    now: datetime,
    status: RunStatus,
    error_code: str,
    error_message: str,
) -> int:
    """把该 Run 仍停在 `running` 的 `node_runs` 行收尾（W3 的兜底）。

    引擎的取消路径（`_run_attempt` 的 `CancelledError` 分支）已经会收尾当场那一行；这里覆盖
    "`node_started` 已 commit、但收尾没机会执行"的窗口（连接被摘掉 / 进程被掐停 / 取消点落在
    span 上下文里）——`node_runs` 不该出现永久 `running`，否则前端节点表一直转圈。
    """
    rows = (
        (
            await session.execute(
                select(NodeRun).where(NodeRun.run_id == workflow_run_id, NodeRun.status == str(RunStatus.RUNNING))
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = str(status)
        row.error_code = error_code
        row.error_message = error_message
        row.ended_at = now
        row.latency_ms = int(_elapsed_seconds(row.started_at, now) * 1000)
    return len(rows)


async def _converge_canceled_run(workflow_run_id: str, api_run_id: str, *, reason: str) -> bool:
    """用**独立 session** 把一行 Run 收敛为 `canceled`（进程收尾 / 排队阶段被取消的兜底，W1/W2）。

    幂等：已经是终态的行不动；失败只记日志（不能因为收敛失败把"进程正在退出"变成异常）。
    """
    try:
        async with get_sessionmaker()() as session:
            now = _utcnow()
            workflow_run = await session.get(WorkflowRun, workflow_run_id)
            if workflow_run is not None and str(workflow_run.status) not in run_service.TERMINAL_STATUSES:
                workflow_run.status = str(RunStatus.CANCELED)
                workflow_run.error_code = str(ErrorCode.RUN_CANCELED)
                workflow_run.error_message = reason
                workflow_run.ended_at = now
                workflow_run.latency_ms = int(_elapsed_seconds(workflow_run.started_at, now) * 1000)
            api_run = await session.get(Run, api_run_id)
            if api_run is not None and str(api_run.status) not in run_service.TERMINAL_STATUSES:
                api_run.status = str(RunStatus.CANCELED)
                api_run.error_code = str(ErrorCode.RUN_CANCELED)
                api_run.error_message = reason
                api_run.canceled_at = now
                api_run.ended_at = now
                api_run.latency_ms = int(_elapsed_seconds(api_run.started_at, now) * 1000)
            await _converge_running_node_runs(
                session,
                workflow_run_id,
                now=now,
                status=RunStatus.CANCELED,
                error_code=str(ErrorCode.RUN_CANCELED),
                error_message=reason,
            )
            await session.commit()
        return True
    except Exception:
        logger.error("workflow.cancel_converge_failed", workflow_run_id=workflow_run_id, exc_info=True)
        return False


async def shutdown_active_runs(*, timeout: float = SHUTDOWN_GRACE_SECONDS) -> int:
    """进程收尾（W2）：取消仍在跑的后台 Run 任务，并给它们一次写终态的机会。

    lifespan 的 `finally` 里、`dispose_engine()` **之前**调用：先取消 + 等收敛、再关连接池 ——
    否则行会停在 `running`，且任务在池关闭后写库会直接报错。返回被取消的任务数（供日志/测试断言）。
    超时未收敛的行由下次启动的 `converge_orphan_runs` 兜底（6.3 第 3 条）。
    """
    pending = [task for task in _BACKGROUND_TASKS if not task.done()]
    if not pending:
        return 0
    for task in pending:
        task.cancel()
    _done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if still_pending:
        logger.warning("workflow.shutdown_timeout", pending=len(still_pending), timeout=timeout)
    logger.info("workflow.shutdown_finished", canceled=len(pending), pending=len(still_pending))
    return len(pending)


def _workflow_error(result: WorkflowResult) -> AppError:
    """引擎返回的失败 → 带原错误码的 `AppError`（finalize 只用它的 `code` / `message`）。"""
    try:
        code = ErrorCode(result.error_code or "")
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    if code is ErrorCode.RUN_CANCELED:
        return RunCanceledError()
    return WorkflowNodeError(code, result.error_message or "Workflow run failed")


def _timeout_error() -> AppError:
    """超 `definition.config.timeout_seconds` → `RUN_TIMEOUT`（4.4.3 的同类语义）。"""
    return RunTimeoutError("Workflow run exceeded the configured timeout")
