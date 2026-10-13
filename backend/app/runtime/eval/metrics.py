"""指标计算（详细设计 4.9.2"写死"的口径；前端 `/stats` 与报告共用同一套字段名）。

| 指标 | 定义 |
|---|---|
| `task_success_rate` | 通过用例数 / 启用用例数（`llm_judge` 按 `score >= threshold`） |
| `tool_success_rate` | 成功 tool span / 全部 tool span（跨所有用例） |
| `workflow_success_rate` | `succeeded` / 全部 workflow_run（**无用例时 `null`**） |
| `avg_latency_ms` / `p95_latency_ms` | 用例级延迟的均值与 P95 |
| `avg_tool_calls` | 用例平均工具调用次数 |
| `avg_total_tokens` | 用例平均 token |
| `total_cost_usd` | 全批估算成本合计（含 judge 调用） |
| `error_rate` | `outcome=error` 占比（error = 执行异常，与 `failed` 区分） |

P95 口径：**nearest-rank**（第 `ceil(0.95 * n)` 个样本，单调、无插值歧义）；`n < 5` 时等于最大值。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.enums import EvalOutcome
from app.runtime.eval.assertions import AssertionResult

P95_RATIO = 0.95


@dataclass(slots=True)
class CaseResult:
    """一条用例的结果：**观察**（latency / tokens / cost / trace）+ **判定**（断言明细）。"""

    case_id: str
    status: str = str(EvalOutcome.PASSED)
    input: str = ""
    weight: float = 1.0
    answer: str = ""
    tool_calls: tuple[str, ...] = ()
    steps: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    trace_id: str | None = None
    run_id: str | None = None
    assertions: tuple[AssertionResult, ...] = ()
    judge_scores: tuple[float, ...] = ()
    judge_cost_usd: Decimal = Decimal(0)
    tool_spans_total: int = 0
    tool_spans_succeeded: int = 0
    workflow_runs: int = 0
    workflow_succeeded: int = 0
    error_code: str | None = None
    error_message: str | None = None
    timed_out: bool = False

    @property
    def outcome(self) -> EvalOutcome:
        """字符串状态 → 枚举（报告与指标统一走枚举）。"""
        return EvalOutcome(self.status)

    @property
    def is_passed(self) -> bool:
        return self.status == str(EvalOutcome.PASSED)

    @property
    def judge_score(self) -> float | None:
        """该用例的 judge 均分（无 judge 断言 → `None`，报告里显示 `—`）。"""
        return round(sum(self.judge_scores) / len(self.judge_scores), 2) if self.judge_scores else None

    @property
    def assertions_passed(self) -> int:
        return sum(1 for item in self.assertions if item.passed)

    def as_dict(self) -> dict[str, Any]:
        """`--json` 与 `eval-*.json` 里的单条用例（DoD：失败用例必须带 `trace_id`）。"""
        body: dict[str, Any] = {
            "case_id": self.case_id,
            "status": self.status,
            "weight": self.weight,
            "latency_ms": self.latency_ms,
            "tool_calls": list(self.tool_calls),
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "cost_usd": float(self.cost_usd),
            "tool_spans": {"total": self.tool_spans_total, "succeeded": self.tool_spans_succeeded},
            "workflow_runs": {"total": self.workflow_runs, "succeeded": self.workflow_succeeded},
            "assertions": [item.as_dict() for item in self.assertions],
            "trace_id": self.trace_id,
            "run_id": self.run_id,
        }
        if self.judge_scores:
            body["judge_scores"] = list(self.judge_scores)
            body["judge_score"] = self.judge_score
            body["judge_cost_usd"] = float(self.judge_cost_usd)
        if self.error_code:
            body["error_code"] = self.error_code
        if self.error_message:
            body["error_message"] = self.error_message
        if self.timed_out:
            body["timed_out"] = True
        return body


@dataclass(frozen=True, slots=True)
class EvalMetrics:
    """一次评测的汇总指标（4.9.2；`None` 表示"该批无样本"，不是 0）。"""

    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    task_success_rate: float = 0.0
    tool_success_rate: float | None = None
    workflow_success_rate: float | None = None
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_tool_calls: float = 0.0
    avg_total_tokens: float = 0.0
    total_cost_usd: Decimal = Decimal(0)
    error_rate: float = 0.0
    avg_judge_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """报告 JSON 与 `--json` 的 `metrics` 段（键名即 4.9.2 的指标名）。"""
        return {
            "total_cases": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "task_success_rate": self.task_success_rate,
            "tool_success_rate": self.tool_success_rate,
            "workflow_success_rate": self.workflow_success_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "avg_tool_calls": self.avg_tool_calls,
            "avg_total_tokens": self.avg_total_tokens,
            "total_cost_usd": float(self.total_cost_usd),
            "error_rate": self.error_rate,
            "avg_judge_score": self.avg_judge_score,
        }


def compute_metrics(results: Sequence[CaseResult]) -> EvalMetrics:
    """按 4.9.2 汇总一批用例结果（空批次返回全 0 / `None`，不抛异常）。"""
    total = len(results)
    if not total:
        return EvalMetrics()
    passed = sum(1 for item in results if item.status == str(EvalOutcome.PASSED))
    failed = sum(1 for item in results if item.status == str(EvalOutcome.FAILED))
    errors = sum(1 for item in results if item.status == str(EvalOutcome.ERROR))
    latencies = [item.latency_ms for item in results]
    tool_total = sum(item.tool_spans_total for item in results)
    tool_succeeded = sum(item.tool_spans_succeeded for item in results)
    workflow_runs = sum(item.workflow_runs for item in results)
    workflow_succeeded = sum(item.workflow_succeeded for item in results)
    judge_scores = [score for item in results for score in item.judge_scores]
    judge_cost = sum((item.judge_cost_usd for item in results), Decimal(0))
    return EvalMetrics(
        total_cases=total,
        passed=passed,
        failed=failed,
        errors=errors,
        task_success_rate=round(passed / total, 4),
        tool_success_rate=round(tool_succeeded / tool_total, 4) if tool_total else None,
        workflow_success_rate=round(workflow_succeeded / workflow_runs, 4) if workflow_runs else None,
        avg_latency_ms=round(sum(latencies) / total, 1),
        p95_latency_ms=round(percentile(latencies, P95_RATIO) or 0.0, 1),
        avg_tool_calls=round(sum(len(item.tool_calls) for item in results) / total, 2),
        avg_total_tokens=round(sum(item.total_tokens for item in results) / total, 1),
        total_cost_usd=sum((item.cost_usd for item in results), Decimal(0)) + judge_cost,
        error_rate=round(errors / total, 4),
        avg_judge_score=round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else None,
    )


def percentile(values: Sequence[float], ratio: float) -> float | None:
    """nearest-rank 百分位（空序列 → `None`）。"""
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    index = max(0, math.ceil(ratio * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def judge_distribution(results: Sequence[CaseResult]) -> tuple[float, ...]:
    """judge 分数分布（4.9.4：报告里要能看到"同一输入分数波动"，仅供人工参考）。"""
    return tuple(sorted(score for item in results for score in item.judge_scores))
