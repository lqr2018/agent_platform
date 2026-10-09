"""一轮对话的编排（详细设计 3.4 / 4.4 / 7.2 任务 6）。

职责边界：

- **请求阶段**（`start_chat`）：校验会话与 Agent、检查同会话活跃 Run（1.5.4）、
  落 user 消息、插入 `runs` 行（`status=running`，2.6），随后**起一个后台任务**执行 Run；
- **执行阶段**（`_execute`）：用**独立 session**（会话/消息/span 的写入不依赖请求生命周期）跑
  `AgentRuntime`，把 SSE 事件推进队列。客户端断开不影响 Run（`DETACH_CANCEL=false`，3.4）；
- **协议阶段**：`api/v1/chat.py` 负责把队列事件编码成 SSE 帧 + 每 15s 心跳（1.2：api 只做协议转换）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings
from app.core.enums import MessageRole, RunKind, RunStatus, SpanStatus, SpanType
from app.core.errors import AgentDisabledError, AppError
from app.core.events import (
    RetrievalCompletedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunStartedPayload,
    SseEventType,
    SseQueueItem,
)
from app.core.logging import get_logger
from app.db.models import Agent
from app.db.models.agent import AGENT_STATUS_ENABLED
from app.db.models.conversation import message_meta
from app.db.models.workflow import TRIGGER_API
from app.db.session import get_sessionmaker
from app.runtime.agent.emitter import EventEmitter, QueueEmitter
from app.runtime.agent.runtime import AgentRuntime
from app.runtime.agent.state import AgentSpec, RunResult
from app.runtime.llm.base import ProviderConfig
from app.runtime.llm.registry import LLMRegistry
from app.runtime.observability.tracer import Tracer
from app.runtime.rag import EMPTY_RETRIEVAL_NOTICE, build_context_blocks
from app.services import (
    agent_service,
    conversation_service,
    kb_service,
    model_provider_service,
    run_service,
    tool_service,
    trace_service,
    workflow_service,
)

logger = get_logger(__name__)

QueueItem = SseQueueItem
"""队列元素：`(事件, payload)`；`None` 是"流结束"哨兵（形状定义在 `core/events.py`）。"""


class ChatRun:
    """一轮对话的句柄（`run_id` 已知，事件从 `queue` 消费）。"""

    def __init__(
        self,
        *,
        run_id: str,
        trace_id: str,
        conversation_id: str,
        queue: asyncio.Queue[QueueItem],
        cancel_event: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.conversation_id = conversation_id
        self.queue = queue
        self.cancel_event = cancel_event
        self.task = task

    async def events(self) -> AsyncIterator[tuple[SseEventType, dict[str, Any]]]:
        """消费事件直到哨兵（结果事件已由服务层保证最后发出）。"""
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


async def start_chat(
    session: AsyncSession,
    settings: Settings,
    *,
    conversation_id: str,
    content: str,
    registry: run_service.RunRegistry | None = None,
) -> ChatRun:
    """请求阶段：校验 → 落库 → 起后台任务（返回句柄供 SSE 端消费）。

    4.4.4 的分叉：`agent.workflow_id` 非空 → 由 `WorkflowEngine` 驱动（Chat 内联 Workflow），
    事件 13/14 走同一条 SSE（3.4）；为空 → 既有的 `AgentRuntime` 直跑路径。
    """
    conversation = await conversation_service.get_conversation(session, conversation_id)
    agent = await agent_service.get_agent(session, conversation.agent_id)
    if agent.status != AGENT_STATUS_ENABLED:
        raise AgentDisabledError(
            f"Agent '{agent.name}' is disabled and cannot start new runs",
            details={"agent_id": agent.id, "status": agent.status},
        )
    run_registry = registry or run_service.get_run_registry(settings)
    if agent.workflow_id:
        return await _start_inline_workflow(
            session,
            settings,
            conversation_id=conversation_id,
            agent=agent,
            content=content,
            registry=run_registry,
        )
    provider = await model_provider_service.get_provider(session, agent.model_provider_id)

    run_id = ids.new_ulid()
    trace_id = ids.new_trace_id()
    cancel_event = run_registry.begin(run_id=run_id, conversation_id=conversation_id)

    user_message = await conversation_service.append_message(
        session,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=content,
        meta=message_meta(
            agent_prompt_version=agent.prompt_version,
            tools=list(agent.tool_ids or ()),
            kb_ids=list(agent.knowledge_base_ids or ()),
        ),
    )
    await run_service.create_run(
        session,
        run_id=run_id,
        agent_id=agent.id,
        conversation_id=conversation_id,
        kind=RunKind.CHAT,
        trace_id=trace_id,
        user_message_id=user_message.id,
        text=content,
    )

    queue: asyncio.Queue[QueueItem] = asyncio.Queue()
    task = asyncio.create_task(
        _execute(
            settings=settings,
            run_id=run_id,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_spec=agent_service.to_spec(agent),
            provider_config=model_provider_service.to_config(provider, settings=settings),
            user_input=content,
            before_seq=user_message.seq,
            cancel_event=cancel_event,
            queue=queue,
            registry=run_registry,
        ),
        name=f"chat-run:{run_id}",
    )
    return ChatRun(
        run_id=run_id,
        trace_id=trace_id,
        conversation_id=conversation_id,
        queue=queue,
        cancel_event=cancel_event,
        task=task,
    )


async def _start_inline_workflow(
    session: AsyncSession,
    settings: Settings,
    *,
    conversation_id: str,
    agent: Agent,
    content: str,
    registry: run_service.RunRegistry,
) -> ChatRun:
    """Chat 内联 Workflow（4.4.4）：Agent 绑定了 Workflow → 由 `SimpleEngine` 驱动该图。

    - user 消息照常落 `messages`（会话是事实来源），`agent` 节点产出的 assistant 消息也进同一会话；
    - `runs.kind=workflow` + `runs.workflow_run_id`，`workflow_runs.trigger=api`；
    - 初始 state 注入 `input`（用户消息）/ `conversation_id` / `agent_id`，图里的节点用
      `{{state.input}}` 之类的模板取用（4.5.2）；
    - 事件流与普通 Chat 完全同构（`run.started` → `workflow.node.*` → `message.*` → 终态），
      因此 `api/v1/chat.py` 与前端不需要任何特判（3.4 事件 13/14 的用途）。
    """
    workflow = await workflow_service.get_workflow(session, agent.workflow_id or "")
    user_message = await conversation_service.append_message(
        session,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=content,
        meta=message_meta(
            agent_prompt_version=agent.prompt_version,
            tools=list(agent.tool_ids or ()),
            kb_ids=list(agent.knowledge_base_ids or ()),
        ),
    )
    queue: asyncio.Queue[QueueItem] = asyncio.Queue()
    handle = await workflow_service.start_run(
        session,
        settings,
        workflow=workflow,
        payload={"input": content, "conversation_id": conversation_id, "agent_id": agent.id},
        trigger=TRIGGER_API,
        agent_id=agent.id,
        conversation_id=conversation_id,
        emit_queue=queue,
        registry=registry,
        run_input={"user_message_id": user_message.id, "text": content},
    )
    logger.info("chat.inline_workflow_started", workflow_id=workflow.id, run_id=handle.api_run_id)
    return ChatRun(
        run_id=handle.api_run_id,
        trace_id=handle.trace_id,
        conversation_id=conversation_id,
        queue=queue,
        cancel_event=handle.cancel_event,
        task=handle.task,
    )


async def _retrieve_context(
    session: AsyncSession,
    settings: Settings,
    *,
    agent_spec: AgentSpec,
    user_input: str,
    emitter: EventEmitter,
    tracer: Tracer,
) -> tuple[list[str] | None, str | None]:
    """4.4.2 第 1 步 / 4.6.3：Agent 绑定 KB 时的**固定预检索**。

    返回 `(注入块, 未命中提示)`，两者互斥：

    - 命中 → 4.4.2 的 `<chunk id source>` 块（`source` 里带 4.6.3 的文件名 / 标题 / 分数）；
    - 未命中或检索不可用 → 4.6.3 的"未检索到相关内容"提示（**不注入空段落**，避免模型编造）。

    检索失败**不打断对话**（知识库是增强而非依赖）：只记日志 + 事件 10 报 `hit_count=0`。
    另外每个 Run 只做一次（在第一次 LLM 调用之前），与 4.4.2 的"仅一次"约定一致；
    另外一条路径（未绑定 KB 时由 `kb_search` 工具按需检索）属迭代 E。
    """
    kb_ids = list(agent_spec.knowledge_base_ids)
    if not kb_ids:
        return None, None
    try:
        async with tracer.span(
            SpanType.RETRIEVER,
            f"retriever:{','.join(kb_ids)}",
            attributes={"kb_ids": kb_ids, "strategy": "prefetch"},
            input={"query": user_input},
        ) as span:
            chunks, used_kb_ids = await kb_service.retrieve(session, kb_ids, user_input, settings=settings)
            span.attributes.update({"hit_count": len(chunks), "used_kb_ids": used_kb_ids})
            span.output = {"chunk_ids": [chunk.chunk_id for chunk in chunks]}
    except AppError as exc:
        logger.warning("chat.retrieval_failed", kb_ids=kb_ids, error_code=str(exc.code))
        await emitter.emit(
            SseEventType.RETRIEVAL_COMPLETED,
            RetrievalCompletedPayload(kb_ids=kb_ids, query=user_input, hit_count=0),
        )
        return None, EMPTY_RETRIEVAL_NOTICE

    await emitter.emit(
        SseEventType.RETRIEVAL_COMPLETED,
        RetrievalCompletedPayload(kb_ids=used_kb_ids, query=user_input, hit_count=len(chunks)),
    )
    blocks = build_context_blocks(chunks)
    return (blocks, None) if blocks else (None, EMPTY_RETRIEVAL_NOTICE)


async def _execute(
    *,
    settings: Settings,
    run_id: str,
    trace_id: str,
    conversation_id: str,
    agent_spec: AgentSpec,
    provider_config: ProviderConfig,
    user_input: str,
    before_seq: int | None,
    cancel_event: asyncio.Event,
    queue: asyncio.Queue[QueueItem],
    registry: run_service.RunRegistry,
) -> None:
    """执行阶段：独立 session + Tracer 落库 + 事件入队（异常一律收敛为终态事件）。"""
    emitter = QueueEmitter(queue)
    try:
        await registry.acquire()
        async with get_sessionmaker()() as session:
            sink = trace_service.DatabaseSpanSink(session)
            tracer = Tracer.from_settings(settings, sink=sink)
            run = await run_service.get_run(session, run_id)
            run_trace = tracer.start_run(
                kind=RunKind.CHAT,
                name=f"chat:{agent_spec.name}",
                run_id=run_id,
                trace_id=trace_id,
                attributes={"agent_id": agent_spec.id, "conversation_id": conversation_id},
            )
            await sink.open_trace(run_trace)
            await emitter.emit(
                SseEventType.RUN_STARTED,
                RunStartedPayload(
                    run_id=run_id, trace_id=trace_id, conversation_id=conversation_id, started_at=_utcnow()
                ),
            )

            runtime = AgentRuntime(
                llm=LLMRegistry(settings).create(provider_config),
                memory=conversation_service.SqlShortTermMemory(session),
                tracer=tracer,
                settings=settings,
                toolkit=tool_service.build_toolkit(session, settings, tool_ids=agent_spec.tool_ids),
            )
            retrieved_context, retrieval_notice = await _retrieve_context(
                session,
                settings,
                agent_spec=agent_spec,
                user_input=user_input,
                emitter=emitter,
                tracer=tracer,
            )
            result = await runtime.run(
                agent_spec,
                user_input,
                conversation_id=conversation_id,
                emit=emitter,
                cancel=cancel_event,
                before_seq=before_seq,
                retrieved_context=retrieved_context,
                retrieval_notice=retrieval_notice,
            )
            await tracer.end_run(
                run_trace,
                status=_span_status_for(result.status),
                error_code=result.error_code,
                error_message=result.error_message,
            )
            await sink.close_trace(
                run_trace,
                status=result.status,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            await run_service.finish_run(session, run, result, output_message_id=result.message_id)
            await _emit_result(emitter, result)
            logger.info(
                "chat.run_finished",
                run_id=run_id,
                trace_id=trace_id,
                status=str(result.status),
                steps=result.steps,
                latency_ms=result.latency_ms,
                error_code=result.error_code,
            )
    except asyncio.CancelledError:  # pragma: no cover - DETACH_CANCEL=true 或进程收尾
        await _mark_failed(settings, run_id=run_id, error=None, status=RunStatus.CANCELED)
        await emitter.emit(
            SseEventType.RUN_FAILED,
            RunFailedPayload(run_id=run_id, error_code="RUN_CANCELED", error_message="Run was canceled"),
        )
        raise
    except AppError as exc:
        await _mark_failed(settings, run_id=run_id, error=exc, status=_status_for(exc))
        await emitter.emit(
            SseEventType.RUN_FAILED,
            RunFailedPayload(run_id=run_id, error_code=str(exc.code), error_message=exc.message),
        )
    except Exception as exc:
        logger.error("chat.run_unexpected_error", run_id=run_id, error_type=type(exc).__name__, exc_info=True)
        await _mark_failed(settings, run_id=run_id, error=None, status=RunStatus.FAILED)
        await emitter.emit(
            SseEventType.RUN_FAILED,
            RunFailedPayload(run_id=run_id, error_code="INTERNAL_ERROR", error_message="Run failed unexpectedly"),
        )
    finally:
        await queue.put(None)
        registry.finish(run_id)
        registry.release()


async def _emit_result(emitter: EventEmitter, result: RunResult) -> None:
    """7.2 任务 6：`run.completed` / `run.failed`（3.4 事件 17/18）。"""
    if result.status == RunStatus.SUCCEEDED:
        await emitter.emit(
            SseEventType.RUN_COMPLETED,
            RunCompletedPayload(
                run_id=result.run_id,
                status=str(result.status),
                steps=result.steps,
                tool_call_count=result.tool_call_count,
                latency_ms=result.latency_ms,
            ),
        )
        return
    await emitter.emit(
        SseEventType.RUN_FAILED,
        RunFailedPayload(
            run_id=result.run_id,
            error_code=result.error_code or "INTERNAL_ERROR",
            error_message=result.error_message or "",
        ),
    )


async def _mark_failed(
    settings: Settings,
    *,
    run_id: str,
    error: AppError | None,
    status: RunStatus,
) -> None:
    """兜底路径：用**新 session** 把 Run 收敛到终态（原 session 可能已不可用）。"""
    try:
        async with get_sessionmaker()() as session:
            run = await run_service.get_run(session, run_id)
            if run.status in run_service.TERMINAL_STATUSES:
                return
            now = _utcnow().replace(tzinfo=None)
            run.status = str(status)
            run.error_code = str(error.code) if error is not None else "INTERNAL_ERROR"
            run.error_message = error.message if error is not None else "Run failed"
            run.ended_at = now
            run.latency_ms = int((now - run.started_at).total_seconds() * 1000)
            if status == RunStatus.CANCELED:
                run.canceled_at = now
            await session.commit()
    except Exception:  # pragma: no cover - 兜底失败只能记日志
        logger.warning("chat.run_finalize_failed", run_id=run_id, exc_info=True)


def _status_for(error: AppError) -> RunStatus:
    return RunStatus.CANCELED if str(error.code) == "RUN_CANCELED" else RunStatus.FAILED


def _span_status_for(status: RunStatus) -> SpanStatus:
    """`RunStatus` → 根 span 的 `SpanStatus`（2.11）。"""
    return SpanStatus.OK if status == RunStatus.SUCCEEDED else SpanStatus.ERROR


def _utcnow() -> datetime:
    return datetime.now(UTC)
