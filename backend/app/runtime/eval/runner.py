"""评测 Runner（详细设计 4.9.1）。

```text
1. 载入 YAML 用例集（name / agent / cases[].input / cases[].expected / cases[].weight）
   并把 agent 配置 + prompt_version 快照写进报告头（保证可复现）
2. 为每个 case 建 Run(kind="eval") + 独立 conversation（避免用例互相污染上下文）
3. 调 AgentRuntime（或 WorkflowEngine，当 agent.workflow_id 非空）
4. 收集产出：answer、tool_calls 序列、latency_ms、total_tokens、total_cost_usd、trace_id
5. 逐条断言（4.9.3）
6. 汇总 metrics（4.9.2），写 data/reports/eval-{ts}.md 与 .json（stdout 同时打印表格）
7. 全程受 max_concurrent_runs 与 eval_concurrency 双重限流；单 case 超时（默认 120s）记 outcome=error
```

**分层**：`EvalRunner` 只做"编排 + 断言 + 汇总"，第 2/3 步（DB、Tracer、LLM、ToolKit 的装配）
由注入的 `CaseExecutor` 完成 —— 这既让单测能脱离 DB 跑（9.2 的替身化要求），也让 Backlog 迭代 D
的 DB 用例来源只需要换一个 `SuiteSource`（4.9.1 的"Runner、指标、断言、报告渲染一行不改"）。

`Run` 的终态**不由断言决定**：断言失败只是"这条没过"（`failed`），执行异常才是 `error`（4.9.3）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.enums import EvalOutcome
from app.core.errors import AppError, ConfigInvalidError
from app.core.logging import get_logger
from app.runtime.eval.assertions import (
    AssertionContext,
    AssertionResult,
    evaluate_assertions,
)
from app.runtime.eval.judges import Judge
from app.runtime.eval.metrics import CaseResult, EvalMetrics, compute_metrics
from app.runtime.eval.suite_source import (
    EvalCase,
    EvalSuite,
    SuiteSource,
    select_cases,
    with_cases,
)

logger = get_logger(__name__)

DEFAULT_CASE_TIMEOUT_SECONDS = 120.0
"""4.9.1 第 7 步：单 case 默认超时（`cases[].timeout_seconds` 可覆盖）。"""

CASE_TIMEOUT_SLACK_SECONDS = 5.0
"""`asyncio.wait_for` 在 executor 自身超时之外再留的余量（让 work 内部先收敛成 `RunTimeoutError`）。"""

MAX_EXIT_CODE = 99
"""退出码上限（`failed + errors` 超过它时截断，避免和 shell 保留码撞车）。"""


@dataclass(slots=True)
class CaseObservation:
    """第 4 步"收集产出"的结果（由 `CaseExecutor` 填）。"""

    answer: str = ""
    tool_calls: tuple[str, ...] = ()
    latency_ms: int = 0
    steps: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    trace_id: str | None = None
    run_id: str | None = None
    tool_spans_total: int = 0
    tool_spans_succeeded: int = 0
    workflow_runs: int = 0
    workflow_succeeded: int = 0


CaseExecutor = Callable[[EvalCase, str, float], Awaitable[CaseObservation]]
"""`(case, agent_id, timeout_seconds) -> CaseObservation`（服务层实现见 `services/eval_service.py`）。"""


@dataclass(slots=True)
class AgentSnapshot:
    """报告头需要的 Agent 快照（4.9.1 第 1 步："保证可复现"）。"""

    id: str
    name: str
    model_name: str
    prompt_version: int | None = None


@dataclass(slots=True)
class EvalRunSummary:
    """一次评测的完整产物（报告渲染与 `--json` 的唯一输入）。"""

    suite: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    concurrency: int
    total_cases: int
    metrics: EvalMetrics
    results: tuple[CaseResult, ...]
    agent_id: str = ""
    agent_name: str = ""
    model_name: str = ""
    prompt_version: int | None = None
    judge_model: str | None = None
    suite_path: Path | None = None
    suite_description: str = ""

    @property
    def tool_span_counts(self) -> tuple[int, int]:
        """`(成功 tool span, 全部 tool span)`（报告里 `92.3%   (12/13)` 的括号内容）。"""
        return (
            sum(item.tool_spans_succeeded for item in self.results),
            sum(item.tool_spans_total for item in self.results),
        )

    @property
    def workflow_run_counts(self) -> tuple[int, int]:
        """`(succeeded workflow_run, 全部 workflow_run)`（无用例 → `(0, 0)` → 报告显示 `n/a`）。"""
        return (
            sum(item.workflow_succeeded for item in self.results),
            sum(item.workflow_runs for item in self.results),
        )

    @property
    def failed_cases(self) -> tuple[CaseResult, ...]:
        return tuple(item for item in self.results if item.status == str(EvalOutcome.FAILED))

    @property
    def errored_cases(self) -> tuple[CaseResult, ...]:
        return tuple(item for item in self.results if item.status == str(EvalOutcome.ERROR))

    @property
    def ok(self) -> bool:
        """是否"全绿"（DoD：失败用例要能在 `/traces` 里点开）。"""
        return not self.failed_cases and not self.errored_cases

    @property
    def exit_code(self) -> int:
        """CLI 退出码：`failed + errors`（0 = 全绿；>99 截断）。"""
        return min(MAX_EXIT_CODE, len(self.failed_cases) + len(self.errored_cases))


class EvalRunner:
    """按 `SuiteSource` 跑一批用例（4.9.1）。

    `execute_case` 由调用方注入（服务层装配 DB/Tracer/LLM；脚本与单测可给一个轻量实现）。
    """

    def __init__(
        self,
        *,
        execute_case: CaseExecutor,
        agent: AgentSnapshot,
        default_timeout_seconds: float = DEFAULT_CASE_TIMEOUT_SECONDS,
        judge: Judge | None = None,
        judge_model: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._execute_case = execute_case
        self._agent = agent
        self._default_timeout_seconds = default_timeout_seconds
        self._judge = judge
        self._judge_model = judge_model or getattr(judge, "model", None)
        self._clock = clock or _utcnow

    @property
    def agent(self) -> AgentSnapshot:
        """报告快照里的 Agent（脚本用它写报告头）。"""
        return self._agent

    async def run_suite(
        self,
        source: SuiteSource,
        *,
        agent_id: str | None = None,
        case_ids: Sequence[str] | None = None,
        concurrency: int = 2,
        on_case_done: Callable[[CaseResult], None] | None = None,
    ) -> EvalRunSummary:
        """跑完整个用例集并返回汇总（**不落盘**：报告由 `runtime/eval/report.py` 负责）。

        - `agent_id`：仅用于对齐"命令行指定的 Agent 与执行器装配的 Agent 是同一个"；
        - `case_ids`：`--cases` 过滤（未知 id → `CONFIG_INVALID`）；
        - `concurrency`：用例级并发（SQLite 是单写者，靠 WAL + 10s `busy_timeout` 串行化写）。
        """
        suite = source.load()
        cases = select_cases(suite, case_ids)
        if not cases:
            raise _no_cases(suite)
        cases_suite = with_cases(suite, cases)
        if agent_id and self._agent.id and agent_id != self._agent.id:
            logger.warning(
                "eval.agent_mismatch",
                requested_agent_id=agent_id,
                executor_agent_id=self._agent.id,
            )
        started_at = self._clock()
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: dict[str, CaseResult] = {}
        async with asyncio.TaskGroup() as group:
            for case in cases:
                group.create_task(self._run_case(case, semaphore, results, on_case_done=on_case_done))
        finished_at = self._clock()
        ordered = tuple(results[case.id] for case in cases if case.id in results)
        metrics = compute_metrics(ordered)
        summary = EvalRunSummary(
            suite=cases_suite.name,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            concurrency=max(1, concurrency),
            total_cases=len(cases),
            metrics=metrics,
            results=ordered,
            agent_id=self._agent.id,
            agent_name=self._agent.name,
            model_name=self._agent.model_name,
            prompt_version=self._agent.prompt_version,
            judge_model=self._judge_model,
            suite_path=cases_suite.source_path,
            suite_description=cases_suite.description,
        )
        logger.info(
            "eval.suite_finished",
            suite=summary.suite,
            cases=summary.total_cases,
            passed=metrics.passed,
            failed=metrics.failed,
            errors=metrics.errors,
            duration_ms=summary.duration_ms,
        )
        return summary

    async def _run_case(
        self,
        case: EvalCase,
        semaphore: asyncio.Semaphore,
        sink: dict[str, CaseResult],
        *,
        on_case_done: Callable[[CaseResult], None] | None,
    ) -> None:
        """单条用例：限流 → 执行（带超时）→ 断言 → 结果（异常一律收敛，不冒泡）。"""
        timeout = case.timeout_seconds or self._default_timeout_seconds
        async with semaphore:
            started = self._clock()
            observation, failure = await self._observe(case, timeout)
            measured_ms = int((self._clock() - started).total_seconds() * 1000)
            latency_ms = observation.latency_ms or measured_ms
            assertions = await evaluate_assertions(
                case.expected,
                AssertionContext(
                    answer=observation.answer,
                    tool_calls=observation.tool_calls,
                    latency_ms=latency_ms,
                    case_id=case.id,
                    user_input=case.input,
                ),
                judge=self._judge,
            )
            result = CaseResult(
                case_id=case.id,
                status=str(_status_for(assertions, failure)),
                input=case.input,
                weight=case.weight,
                answer=observation.answer,
                tool_calls=observation.tool_calls,
                steps=observation.steps,
                total_tokens=observation.total_tokens,
                cost_usd=observation.cost_usd,
                latency_ms=latency_ms,
                trace_id=observation.trace_id,
                run_id=observation.run_id,
                assertions=assertions,
                judge_scores=tuple(item.score for item in assertions if item.score is not None),
                judge_cost_usd=sum((item.cost_usd for item in assertions), Decimal(0)),
                tool_spans_total=observation.tool_spans_total,
                tool_spans_succeeded=observation.tool_spans_succeeded,
                workflow_runs=observation.workflow_runs,
                workflow_succeeded=observation.workflow_succeeded,
                error_code=failure[0] if failure else None,
                error_message=failure[1] if failure else None,
                timed_out=bool(failure and failure[2]),
            )
            sink[case.id] = result
            if on_case_done is not None:
                on_case_done(result)
            logger.info(
                "eval.case_finished",
                suite_case=case.id,
                status=result.status,
                latency_ms=result.latency_ms,
                trace_id=result.trace_id,
            )

    async def _observe(self, case: EvalCase, timeout: float) -> tuple[CaseObservation, tuple[str, str, bool] | None]:
        """执行一条用例并收敛异常：返回 `(观察, 失败三元组 (code, message, timed_out) | None)`。"""
        try:
            observation = await asyncio.wait_for(
                self._execute_case(case, self._agent.id, timeout),
                timeout=timeout + CASE_TIMEOUT_SLACK_SECONDS,
            )
        except TimeoutError:
            logger.warning("eval.case_timeout", suite_case=case.id, timeout_seconds=timeout)
            return CaseObservation(), ("RUN_TIMEOUT", f"case exceeded {timeout:g}s", True)
        except AppError as exc:
            return CaseObservation(), (str(exc.code), exc.message, False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 执行异常 = outcome:error（4.9.1 第 7 步）
            logger.error("eval.case_failed", suite_case=case.id, error_type=type(exc).__name__, exc_info=True)
            return CaseObservation(), ("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", False)
        return observation, None


def _status_for(assertions: Sequence[AssertionResult], failure: tuple[str, str, bool] | None) -> EvalOutcome:
    """4.9.3：执行异常 → `error`；有"求值不了"的断言 → `error`；有没过的 → `failed`；否则 `passed`。"""
    if failure is not None or any(item.errored for item in assertions):
        return EvalOutcome.ERROR
    if any(not item.passed for item in assertions):
        return EvalOutcome.FAILED
    return EvalOutcome.PASSED


def _no_cases(suite: EvalSuite) -> AppError:
    """`--cases` 过滤后为空（或全被 `enabled: false` 关掉）→ `CONFIG_INVALID`。"""
    return ConfigInvalidError(
        f"Suite {suite.name!r} has no enabled cases to run",
        details={"suite": suite.name, "cases": [case.id for case in suite.cases]},
    )


def _utcnow() -> datetime:
    """报告时间戳（aware UTC；报告是给人看的文件，不沿用 DB 的 naive 约定）。"""
    return datetime.now(UTC)
