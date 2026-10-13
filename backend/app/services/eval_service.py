"""评测的服务层装配（详细设计 4.9.1 第 2/3 步 + 7.8 任务 2）。

`EvalRunner` 只认"用例 → 产出"这一个函数（`runtime/eval/runner.py::CaseExecutor`），
本模块负责把这件事接上平台：**每个 case 一个独立 conversation + 一条 `Run(kind=eval)`**（4.9.1
第 2 步：用例之间不共享上下文），Chat 有的东西一个都不少：

- `Tracer` + `DatabaseSpanSink`：`runs`/`spans` 照常落库（4.9.1 末条：报告里没过的那条能点开 trace 看）；
- KB 预检索（复用 `chat_service.retrieve_context`，4.4.2 / 4.6.3 的"固定预检索"语义）；
- `agent.workflow_id` 非空 → 走 `WorkflowEngine`（4.9.1 第 3 步：或 WorkflowEngine）；
- `RunRegistry` 的全局并发闸（4.9.1 第 7 步：与 `eval_concurrency` 双重限流）。

观测值（`tool_calls` 序列 / tool span 成败 / workflow_run 成败）**从库里读回**：
`Runtime` 的返回值里没有 span 明细，而 4.9.2 的 `tool_success_rate` / `workflow_success_rate`
要"跨所有用例"统计，读库比在内存里拼更少歧义。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings
from app.core.enums import MessageRole, RunKind, RunStatus, SpanStatus, SpanType
from app.core.errors import AgentDisabledError, AgentNotFoundError, AppError, ConfigInvalidError
from app.core.logging import get_logger
from app.db.models import Agent
from app.db.models.agent import AGENT_STATUS_ENABLED
from app.db.models.conversation import Run, message_meta
from app.db.models.tool import ToolInvocation
from app.db.models.trace import Span
from app.db.models.workflow import TRIGGER_API, WorkflowRun
from app.db.session import get_sessionmaker
from app.runtime.agent.emitter import NullEmitter
from app.runtime.agent.runtime import AgentRuntime
from app.runtime.agent.state import AgentSpec
from app.runtime.eval.judges import Judge, LLMJudge
from app.runtime.eval.runner import (
    AgentSnapshot,
    CaseExecutor,
    CaseObservation,
    EvalRunner,
    EvalRunSummary,
)
from app.runtime.eval.suite_source import EvalCase, EvalSuite
from app.runtime.llm.base import ProviderConfig
from app.runtime.llm.registry import LLMRegistry
from app.runtime.observability.tracer import Tracer
from app.schemas.conversation import ConversationCreate
from app.services import (
    agent_service,
    chat_service,
    conversation_service,
    model_provider_service,
    run_service,
    tool_service,
    trace_service,
    workflow_service,
)

logger = get_logger(__name__)

EVAL_CONVERSATION_PREFIX = "eval:"
"""评测会话标题前缀（`/conversations` 里一眼能看出是评测产物）。"""


@dataclass(slots=True)
class EvalSetup:
    """一次评测的装配结果（报告头快照 + 执行器 + judge）。"""

    suite: EvalSuite
    agent: AgentSnapshot
    agent_spec: AgentSpec
    provider_config: ProviderConfig
    executor: CaseExecutor
    judge: Judge | None = None
    judge_model: str | None = None


class DatabaseCaseExecutor:
    """把一个 `EvalCase` 跑成 `CaseObservation`（4.9.1 第 2–4 步的 DB 装配）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        agent: Agent,
        agent_spec: AgentSpec,
        provider_config: ProviderConfig,
        suite: str,
        registry: run_service.RunRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._agent = agent
        self._agent_spec = agent_spec
        self._provider_config = provider_config
        self._suite = suite
        self._registry = registry

    async def __call__(self, case: EvalCase, agent_id: str, timeout_seconds: float) -> CaseObservation:
        """4.9.1 第 2–4 步：建会话/消息/Run → 跑 Agent（或 Workflow）→ 读回产出。"""
        registry = self._registry or run_service.get_run_registry(self._settings)
        async with get_sessionmaker()() as session:
            conversation = await conversation_service.create_conversation(
                session,
                ConversationCreate(agent_id=self._agent.id, title=f"{EVAL_CONVERSATION_PREFIX}{self._suite}:{case.id}"),
            )
            user_message = await conversation_service.append_message(
                session,
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=case.input,
                meta=message_meta(
                    agent_prompt_version=self._agent.prompt_version,
                    tools=list(self._agent.tool_ids or ()),
                    kb_ids=list(self._agent.knowledge_base_ids or ()),
                ),
            )
            if self._agent.workflow_id:
                return await self._run_workflow(
                    registry,
                    case=case,
                    conversation_id=conversation.id,
                    user_message_id=user_message.id,
                )
            return await self._run_agent(
                registry,
                case=case,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                before_seq=user_message.seq,
            )

    async def _run_agent(
        self,
        registry: run_service.RunRegistry,
        *,
        case: EvalCase,
        conversation_id: str,
        user_message_id: str,
        before_seq: int | None,
    ) -> CaseObservation:
        """走 `AgentRuntime` 直跑路径（与 `chat_service._execute` 同构，只是不发 SSE）。"""
        run_id = ids.new_ulid()
        trace_id = ids.new_trace_id()
        cancel_event = registry.begin(run_id=run_id, conversation_id=conversation_id)
        await registry.acquire()
        try:
            async with get_sessionmaker()() as session:
                run = await run_service.create_run(
                    session,
                    run_id=run_id,
                    agent_id=self._agent.id,
                    conversation_id=conversation_id,
                    kind=RunKind.EVAL,
                    trace_id=trace_id,
                    user_message_id=user_message_id,
                    text=case.input,
                )
                sink = trace_service.DatabaseSpanSink(session)
                tracer = Tracer.from_settings(self._settings, sink=sink)
                run_trace = tracer.start_run(
                    kind=RunKind.EVAL,
                    name=f"{EVAL_CONVERSATION_PREFIX}{self._suite}:{case.id}",
                    run_id=run_id,
                    trace_id=trace_id,
                    attributes={
                        "agent_id": self._agent.id,
                        "conversation_id": conversation_id,
                        "suite": self._suite,
                        "case_id": case.id,
                    },
                )
                await sink.open_trace(run_trace)
                emitter = NullEmitter()
                runtime = AgentRuntime(
                    llm=LLMRegistry(self._settings).create(self._provider_config),
                    memory=conversation_service.SqlShortTermMemory(session),
                    tracer=tracer,
                    settings=self._settings,
                    toolkit=tool_service.build_toolkit(session, self._settings, tool_ids=self._agent_spec.tool_ids),
                )
                retrieved_context, retrieval_notice = await chat_service.retrieve_context(
                    session,
                    self._settings,
                    agent_spec=self._agent_spec,
                    user_input=case.input,
                    emitter=emitter,
                    tracer=tracer,
                )
                try:
                    result = await runtime.run(
                        self._agent_spec,
                        case.input,
                        conversation_id=conversation_id,
                        emit=emitter,
                        cancel=cancel_event,
                        before_seq=before_seq,
                        retrieved_context=retrieved_context,
                        retrieval_notice=retrieval_notice,
                    )
                except AppError as exc:
                    await tracer.end_run(run_trace, status=SpanStatus.ERROR, error_code=str(exc.code))
                    await sink.close_trace(run_trace, status=RunStatus.FAILED, error_code=str(exc.code))
                    await _finalize_failed(session, run, exc)
                    raise
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
                return await self._observe(session, run=run, trace_id=trace_id, fallback_latency_ms=result.latency_ms)
        finally:
            registry.finish(run_id)
            registry.release()

    async def _run_workflow(
        self,
        registry: run_service.RunRegistry,
        *,
        case: EvalCase,
        conversation_id: str,
        user_message_id: str,
    ) -> CaseObservation:
        """Agent 绑定了 Workflow → 走 `WorkflowEngine`（4.9.1 第 3 步；`begin_registry=True`）。

        并发闸与收尾都由引擎的 `_execute` 负责（`registry.acquire()` / `finish()` / `release()`），
        这里只等它跑完再把产出读回来（评测是同步语义，和 Chat 的"后台跑 + SSE"不同）。
        """
        async with get_sessionmaker()() as session:
            workflow = await workflow_service.get_workflow(session, self._agent.workflow_id or "")
            handle = await workflow_service.start_run(
                session,
                self._settings,
                workflow=workflow,
                payload={"input": case.input, "conversation_id": conversation_id, "agent_id": self._agent.id},
                trigger=TRIGGER_API,
                agent_id=self._agent.id,
                conversation_id=conversation_id,
                registry=registry,
                run_input={"user_message_id": user_message_id, "text": case.input},
            )
            await handle.task
            api_run = await run_service.get_run(session, handle.api_run_id)
            return await self._observe(
                session,
                run=api_run,
                trace_id=handle.trace_id,
                workflow_run_id=api_run.workflow_run_id,
            )

    async def _observe(
        self,
        session: AsyncSession,
        *,
        run: Run,
        trace_id: str,
        fallback_latency_ms: int = 0,
        workflow_run_id: str | None = None,
    ) -> CaseObservation:
        """4.9.1 第 4 步：把产出从库里读回来（`runs` + `tool_invocations` + `spans` + `workflow_runs`）。"""
        names = (
            await session.execute(
                select(ToolInvocation.tool_name)
                .where(ToolInvocation.run_id == run.id)
                .order_by(ToolInvocation.step_index, ToolInvocation.call_index)
            )
        ).scalars()
        span_total, span_ok = (
            await session.execute(
                select(
                    func.count(Span.id),
                    func.sum(case((Span.status == str(SpanStatus.OK), 1), else_=0)),
                ).where(Span.trace_id == trace_id, Span.span_type == str(SpanType.TOOL))
            )
        ).one()
        workflow_total, workflow_ok = 0, 0
        if workflow_run_id:
            workflow_run = await session.get(WorkflowRun, workflow_run_id)
            if workflow_run is not None:
                workflow_total = 1
                workflow_ok = 1 if workflow_run.status == str(RunStatus.SUCCEEDED) else 0
        output = run.output if isinstance(run.output, dict) else {}
        return CaseObservation(
            answer=str(output.get("text") or ""),
            tool_calls=tuple(str(name) for name in names.all()),
            latency_ms=int(run.latency_ms or fallback_latency_ms or 0),
            steps=int(run.steps or 0),
            total_tokens=int(run.total_tokens or 0),
            cost_usd=Decimal(str(run.total_cost_usd or 0)),
            trace_id=trace_id,
            run_id=run.id,
            tool_spans_total=int(span_total or 0),
            tool_spans_succeeded=int(span_ok or 0),
            workflow_runs=workflow_total,
            workflow_succeeded=workflow_ok,
        )


async def resolve_agent(session: AsyncSession, reference: str) -> Agent:
    """按 id（ULID）或 name 精确匹配取 Agent（`--agent <id|name>` 两种写法都支持）。"""
    try:
        return await agent_service.get_agent(session, reference)
    except AgentNotFoundError:
        agent = (
            (await session.execute(select(Agent).where(Agent.name == reference, Agent.deleted_at.is_(None))))
            .scalars()
            .first()
        )
        if agent is None:
            raise AgentNotFoundError(f"Agent '{reference}' does not exist") from None
        return agent


def suite_needs_judge(suite: EvalSuite) -> bool:
    """用例集里有没有 `llm_judge` 断言（没有就不装配 judge，省一次 LLM 客户端创建）。"""
    return any(expectation.type == "llm_judge" for case in suite.cases for expectation in case.expected)


def build_judge(
    settings: Settings,
    *,
    provider_config: ProviderConfig,
    model_name: str,
    judge_model: str | None = None,
) -> LLMJudge:
    """按 Agent 的 Provider 装配 judge（4.9.3：judge 模型可在评测级覆盖，默认同 Agent 模型）。"""
    return LLMJudge(
        LLMRegistry(settings).create(provider_config),
        model=judge_model or model_name,
    )


async def build_setup(
    session: AsyncSession,
    settings: Settings,
    *,
    suite: EvalSuite,
    agent_ref: str | None = None,
    judge_model: str | None = None,
    registry: run_service.RunRegistry | None = None,
    executor: CaseExecutor | None = None,
) -> EvalSetup:
    """把"跑这个用例集"需要的一切装配好（4.9.1 第 1 步的快照 + 执行器 + judge）。"""
    reference = agent_ref or suite.agent
    if not reference:
        raise ConfigInvalidError(
            f"Suite {suite.name!r} does not declare an agent and --agent was not given",
            details={"suite": suite.name},
        )
    agent = await resolve_agent(session, reference)
    if agent.status != AGENT_STATUS_ENABLED:
        raise AgentDisabledError(
            f"Agent '{agent.name}' is disabled and cannot be evaluated",
            details={"agent_id": agent.id, "status": agent.status},
        )
    provider = await model_provider_service.get_provider(session, agent.model_provider_id)
    provider_config = model_provider_service.to_config(provider, settings=settings)
    agent_spec = agent_service.to_spec(agent)
    judge = (
        build_judge(settings, provider_config=provider_config, model_name=agent.model_name, judge_model=judge_model)
        if suite_needs_judge(suite)
        else None
    )
    return EvalSetup(
        suite=suite,
        agent=AgentSnapshot(
            id=agent.id,
            name=agent.name,
            model_name=agent.model_name,
            prompt_version=int(agent.prompt_version),
        ),
        agent_spec=agent_spec,
        provider_config=provider_config,
        executor=executor
        or DatabaseCaseExecutor(
            settings,
            agent=agent,
            agent_spec=agent_spec,
            provider_config=provider_config,
            suite=suite.name,
            registry=registry,
        ),
        judge=judge,
        judge_model=judge.model if isinstance(judge, LLMJudge) else judge_model,
    )


def make_runner(setup: EvalSetup, *, default_timeout_seconds: float | None = None) -> EvalRunner:
    """装配 `EvalRunner`（脚本与集成测试共用这一条装配路径）。"""
    kwargs: dict[str, Any] = {}
    if default_timeout_seconds is not None:
        kwargs["default_timeout_seconds"] = default_timeout_seconds
    return EvalRunner(
        execute_case=setup.executor,
        agent=setup.agent,
        judge=setup.judge,
        judge_model=setup.judge_model,
        **kwargs,
    )


async def run_suite(
    session: AsyncSession,
    settings: Settings,
    *,
    suite: EvalSuite,
    agent_ref: str | None = None,
    judge_model: str | None = None,
    case_ids: Sequence[str] | None = None,
    concurrency: int | None = None,
    registry: run_service.RunRegistry | None = None,
    executor: CaseExecutor | None = None,
    default_timeout_seconds: float | None = None,
) -> EvalRunSummary:
    """装配 + 跑完整套（脚本与集成测试的入口；报告落盘由调用方决定）。"""
    setup = await build_setup(
        session,
        settings,
        suite=suite,
        agent_ref=agent_ref,
        judge_model=judge_model,
        registry=registry,
        executor=executor,
    )
    runner = make_runner(setup, default_timeout_seconds=default_timeout_seconds)
    return await runner.run_suite(
        _LoadedSuite(suite),
        agent_id=setup.agent.id,
        case_ids=case_ids,
        concurrency=concurrency if concurrency is not None else settings.eval_concurrency,
    )


@dataclass(frozen=True, slots=True)
class _LoadedSuite:
    """把已载入的 `EvalSuite` 当成 `SuiteSource` 用（避免重复读 YAML）。"""

    suite: EvalSuite

    def load(self) -> EvalSuite:
        return self.suite


async def _finalize_failed(session: AsyncSession, run: Run, error: AppError) -> None:
    """把 Run 收成 `failed`（与 `chat_service._mark_failed` 同策略：进终态后不再改）。"""
    if run.status in run_service.TERMINAL_STATUSES:
        return
    now = _utcnow()
    run.status = str(RunStatus.FAILED)
    run.error_code = str(error.code)
    run.error_message = error.message
    run.ended_at = now
    run.latency_ms = int((now - run.started_at).total_seconds() * 1000) if run.started_at else 0
    await session.commit()


def _span_status_for(status: RunStatus) -> SpanStatus:
    """`RunStatus` → 根 span 的 `SpanStatus`（4.8.1）。"""
    return SpanStatus.OK if status == RunStatus.SUCCEEDED else SpanStatus.ERROR


def _utcnow() -> datetime:
    """naive UTC（与 `db/base.now_utc` / `run_service._utcnow` 一致，0.2.1）。"""
    return datetime.now(UTC).replace(tzinfo=None)
