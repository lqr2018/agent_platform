"""指标口径与结果对象的单测（详细设计 4.9.2；`app/runtime/eval/metrics.py`）。

两条硬约束（前端 `/stats` 与报告共用同一套字段名/口径）：

1. **P95 = nearest-rank**（第 `ceil(0.95 * n)` 个样本，无插值）：`n < 5` 时等于最大值；
2. `tool_success_rate` / `workflow_success_rate` 在**没有样本时是 `None`（`n/a`）而不是 0**
   —— "没跑过 workflow" 与 "workflow 全挂" 不能混为一谈。
"""

from __future__ import annotations

from decimal import Decimal

from app.runtime.eval import (
    AssertionResult,
    CaseResult,
    EvalMetrics,
    compute_metrics,
    judge_distribution,
    percentile,
)

METRIC_KEYS = {
    "total_cases",
    "passed",
    "failed",
    "errors",
    "task_success_rate",
    "tool_success_rate",
    "workflow_success_rate",
    "avg_latency_ms",
    "p95_latency_ms",
    "avg_tool_calls",
    "avg_total_tokens",
    "total_cost_usd",
    "error_rate",
    "avg_judge_score",
}


def _case(
    case_id: str,
    *,
    status: str = "passed",
    latency_ms: int = 0,
    tool_calls: tuple[str, ...] = (),
    total_tokens: int = 0,
    cost: str = "0",
    tool_spans: tuple[int, int] = (0, 0),
    workflow: tuple[int, int] = (0, 0),
    judge_scores: tuple[float, ...] = (),
    judge_cost: str = "0",
) -> CaseResult:
    """造一条用例结果（`tool_spans` / `workflow` 是 `(succeeded, total)`）。"""
    return CaseResult(
        case_id=case_id,
        status=status,
        latency_ms=latency_ms,
        tool_calls=tool_calls,
        total_tokens=total_tokens,
        cost_usd=Decimal(cost),
        tool_spans_succeeded=tool_spans[0],
        tool_spans_total=tool_spans[1],
        workflow_succeeded=workflow[0],
        workflow_runs=workflow[1],
        judge_scores=judge_scores,
        judge_cost_usd=Decimal(judge_cost),
    )


def test_percentile_is_nearest_rank() -> None:
    """第 `ceil(0.95 * n)` 个样本；输入乱序也要先排序。"""
    assert percentile([], 0.95) is None
    assert percentile([5], 0.95) == 5.0
    assert percentile([1, 2, 3, 4], 0.95) == 4.0, "n < 5 → 最大值"
    assert percentile([100, 1, 50], 0.95) == 100.0
    assert percentile(list(range(1, 21)), 0.95) == 19.0, "ceil(19) = 19 → 第 19 个样本"
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3.0
    assert percentile([1200], 0.95) == 1200.0
    assert percentile([1, 2], 0.0) == 1.0, "`ceil(0) - 1` 收敛到第 1 个样本，不越界"


def test_compute_metrics_batch() -> None:
    results = [
        _case(
            "a",
            latency_ms=100,
            tool_calls=("calculator",),
            total_tokens=10,
            cost="0.01",
            tool_spans=(1, 1),
            judge_scores=(0.8,),
            judge_cost="0.002",
        ),
        _case("b", latency_ms=200, total_tokens=20, cost="0.02", tool_spans=(0, 1)),
        _case("c", status="failed", latency_ms=300, total_tokens=30, workflow=(1, 1)),
        _case("d", status="error", latency_ms=400, total_tokens=40, cost="0.04", tool_spans=(2, 2)),
        _case("e", latency_ms=5000, total_tokens=50, cost="0.05", workflow=(0, 1)),
    ]
    metrics = compute_metrics(results)

    assert (metrics.total_cases, metrics.passed, metrics.failed, metrics.errors) == (5, 3, 1, 1)
    assert metrics.task_success_rate == 0.6
    assert metrics.error_rate == 0.2
    assert metrics.tool_success_rate == 0.75, "3 成功 / 4 个 tool span"
    assert metrics.workflow_success_rate == 0.5, "1 成功 / 2 个 workflow_run"
    assert metrics.avg_latency_ms == 1200.0
    assert metrics.p95_latency_ms == 5000.0
    assert metrics.avg_tool_calls == 0.2
    assert metrics.avg_total_tokens == 30.0
    assert metrics.total_cost_usd == Decimal("0.122"), "含 judge 的 0.002"
    assert metrics.avg_judge_score == 0.8


def test_rate_metrics_are_null_without_samples() -> None:
    """没跑过 tool / workflow 时是 `n/a`（`None`），不是 0%。"""
    metrics = compute_metrics([_case("only", latency_ms=10)])
    assert metrics.tool_success_rate is None
    assert metrics.workflow_success_rate is None
    assert metrics.avg_judge_score is None
    assert metrics.task_success_rate == 1.0


def test_compute_metrics_empty_batch_is_zeroed_not_raising() -> None:
    metrics = compute_metrics([])
    assert metrics == EvalMetrics()
    assert metrics.as_dict()["task_success_rate"] == 0.0
    assert metrics.as_dict()["tool_success_rate"] is None


def test_metrics_as_dict_keys_match_design_contract() -> None:
    """键名即 4.9.2 的指标名（前端 `/stats` 与报告都按这套名字取数）。"""
    assert set(EvalMetrics().as_dict()) == METRIC_KEYS
    assert isinstance(EvalMetrics().as_dict()["total_cost_usd"], float), "JSON 里成本是 float"


def test_judge_distribution_is_sorted_across_cases() -> None:
    results = [
        _case("a", judge_scores=(0.4, 0.9)),
        _case("b", judge_scores=(0.1,)),
        _case("c"),
    ]
    assert judge_distribution(results) == (0.1, 0.4, 0.9)
    assert compute_metrics(results).avg_judge_score == 0.47, "均值四舍五入到 2 位"


# ------------------------------------------------------------- CaseResult


def test_case_result_helpers() -> None:
    passed = AssertionResult(type="contains", passed=True)
    failed = AssertionResult(type="contains", passed=False, errored=True)
    result = CaseResult(
        case_id="c1",
        status="failed",
        assertions=(passed, failed, passed),
        judge_scores=(0.5, 0.75),
        trace_id="tr_1",
    )

    assert result.outcome == "failed"
    assert result.is_passed is False
    assert result.assertions_passed == 2, "`errored` 的断言不算通过"
    assert result.judge_score == 0.62, "同一用例多条 judge 断言取均分（2 位小数）"
    assert CaseResult(case_id="c2").judge_score is None


def test_case_result_as_dict_carries_trace_and_error_fields() -> None:
    """DoD：失败用例必须带 `trace_id`，才能在 `/traces` 里点开复盘。"""
    body = _case("c1", status="failed", latency_ms=1500, tool_calls=("calculator",)).as_dict()
    assert body["case_id"] == "c1"
    assert body["trace_id"] is None
    assert body["run_id"] is None
    assert body["tool_calls"] == ["calculator"]
    assert body["tool_spans"] == {"total": 0, "succeeded": 0}
    assert "error_code" not in body, "成功的用例不塞错误字段"
    assert "judge_scores" not in body, "没有 judge 断言时不出现 judge 字段"

    errored = CaseResult(
        case_id="c2",
        status="error",
        trace_id="tr_2",
        run_id="run_2",
        error_code="RUN_TIMEOUT",
        error_message="case exceeded 0.05s",
        timed_out=True,
        judge_scores=(0.9,),
        judge_cost_usd=Decimal("0.0004"),
    ).as_dict()
    assert errored["error_code"] == "RUN_TIMEOUT"
    assert errored["error_message"] == "case exceeded 0.05s"
    assert errored["timed_out"] is True
    assert errored["judge_scores"] == [0.9]
    assert errored["judge_score"] == 0.9
    assert errored["judge_cost_usd"] == 0.0004
