"""Runner 编排 + 报告落盘的单测（详细设计 4.9.1 / 4.9.2 / 4.9.4）。

`EvalRunner` 只做"编排 + 断言 + 汇总"，执行侧由注入的 `CaseExecutor` 负责（9.2 的替身化要求），
所以这里用脚本化执行器 + 内存 `SuiteSource` 覆盖：

- 三种终态的归位：`passed` / `failed`（断言没过）/ `error`（执行异常 **或** 断言求值不了）；
- 单 case 超时、`AppError`、未知异常各自的错误码与 `timed_out` 标记；
- `--cases` 过滤、空集报错、并发上限、`on_case_done` 回调与结果顺序；
- 报告：`.md` + `.json` 落盘、`--json` 与落盘同源（`summary_to_dict`）、`REPORT_KEEP` 轮转。

时钟固定（`clock=`）→ 报告文件名与 `duration_ms` 都可复现。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.runtime.eval.runner as runner_module
from app.core.errors import ConfigInvalidError, ModelTimeoutError, RunTimeoutError
from app.runtime.eval import (
    REPORT_KEEP,
    AgentSnapshot,
    CaseObservation,
    EvalCase,
    EvalExpectation,
    EvalRunner,
    EvalSuite,
    prune_reports,
    render_markdown,
    report_paths,
    summary_to_dict,
    write_report,
)

AGENT = AgentSnapshot(id="ag_1", name="eval-agent", model_name="fake-model", prompt_version=3)
NOW = datetime(2026, 10, 12, 9, 30, 0, tzinfo=UTC)
"""固定时钟（报告文件名 `eval-20261012-093000`）。"""

CONTAINS_72 = EvalExpectation(type="contains", value="72")
CONTAINS_NOPE = EvalExpectation(type="contains", value="nope")
MAX_LATENCY_50 = EvalExpectation(type="max_latency", value_ms=50)
JUDGE = EvalExpectation(type="llm_judge", rubric="必须给出 72")


def _clock() -> datetime:
    return NOW


def _case(
    case_id: str,
    *expected: EvalExpectation,
    enabled: bool = True,
    timeout_seconds: float | None = None,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        input=f"ask {case_id}",
        expected=expected or (CONTAINS_72,),
        enabled=enabled,
        timeout_seconds=timeout_seconds,
    )


class _Source:
    """内存用例来源（`SuiteSource` 是个 Protocol，Backlog 的 DB 来源同形）。"""

    def __init__(self, suite: EvalSuite) -> None:
        self._suite = suite

    def load(self) -> EvalSuite:
        return self._suite


class _ScriptedExecutor:
    """按 case id 返回脚本化观察 / 抛脚本化异常；顺带记录并发峰值与收到的超时。"""

    def __init__(self, script: dict[str, Any], *, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.seen: list[str] = []
        self.timeouts: dict[str, float] = {}
        self.active = 0
        self.max_active = 0

    async def __call__(self, case: EvalCase, agent_id: str, timeout: float) -> CaseObservation:
        self.seen.append(case.id)
        self.timeouts[case.id] = timeout
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            outcome = self.script[case.id]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            self.active -= 1


def _runner(execute_case: Any, **overrides: Any) -> EvalRunner:
    """构造 Runner（默认固定时钟；`overrides` 可换 judge / timeout / agent）。"""
    options: dict[str, Any] = {"execute_case": execute_case, "agent": AGENT, "clock": _clock}
    options.update(overrides)
    return EvalRunner(**options)


# ------------------------------------------------------------- 三种终态


async def test_run_suite_places_cases_in_three_states() -> None:
    suite = EvalSuite(
        name="contract",
        description="unit",
        cases=(
            _case("ok", CONTAINS_72, MAX_LATENCY_50),
            _case("bad", CONTAINS_72),
            _case("boom", CONTAINS_72),
            _case("judged", JUDGE),
            _case("disabled", CONTAINS_72, enabled=False),
        ),
    )
    executor = _ScriptedExecutor(
        {
            "ok": CaseObservation(
                answer="72",
                tool_calls=("calculator",),
                latency_ms=30,
                total_tokens=12,
                tool_spans_total=1,
                tool_spans_succeeded=1,
                workflow_runs=1,
                workflow_succeeded=1,
                trace_id="tr_ok",
                run_id="run_ok",
            ),
            "bad": CaseObservation(answer="别的答案", latency_ms=40, trace_id="tr_bad"),
            "boom": ModelTimeoutError("upstream timed out"),
            "judged": CaseObservation(answer="72", latency_ms=10, trace_id="tr_judged"),
        },
    )
    done: list[str] = []
    summary = await _runner(executor).run_suite(
        _Source(suite),
        agent_id=AGENT.id,
        concurrency=2,
        on_case_done=lambda result: done.append(result.case_id),
    )

    assert executor.seen == ["ok", "bad", "boom", "judged"], "`enabled: false` 的用例不执行"
    assert sorted(done) == ["bad", "boom", "judged", "ok"], "每条用例完成时回调一次"
    assert [item.case_id for item in summary.results] == ["ok", "bad", "boom", "judged"]
    assert summary.suite == "contract"
    assert summary.total_cases == 4
    assert (summary.concurrency, summary.duration_ms) == (2, 0)
    assert (summary.agent_id, summary.agent_name, summary.model_name) == ("ag_1", "eval-agent", "fake-model")
    assert summary.prompt_version == 3
    assert summary.judge_model is None, "没注入 judge → 报告头显示未使用"

    by_id = {item.case_id: item for item in summary.results}
    assert by_id["ok"].status == "passed"
    assert by_id["ok"].latency_ms == 30
    assert by_id["ok"].trace_id == "tr_ok"
    assert by_id["ok"].run_id == "run_ok"
    assert by_id["ok"].assertions_passed == 2

    assert by_id["bad"].status == "failed", "断言没过只是 failed（Run 本身成功）"
    assert by_id["bad"].trace_id == "tr_bad", "DoD：失败用例必须带 trace_id 才能复盘"
    assert by_id["bad"].error_code is None
    assert by_id["bad"].assertions[0].detail == "answer does not contain '72'", "观察里的延迟要带进断言上下文"

    assert by_id["boom"].status == "error"
    assert by_id["boom"].error_code == "MODEL_TIMEOUT"
    assert by_id["boom"].error_message == "upstream timed out"
    assert by_id["boom"].timed_out is False

    assert by_id["judged"].status == "error", "断言求值不了 → error（4.9.3）"
    assert by_id["judged"].error_code is None
    assert by_id["judged"].assertions[0].errored is True
    assert by_id["judged"].judge_scores == ()

    metrics = summary.metrics
    assert (metrics.passed, metrics.failed, metrics.errors) == (1, 1, 2)
    assert metrics.task_success_rate == 0.25
    assert metrics.error_rate == 0.5
    assert metrics.avg_latency_ms == 20.0, "取到观察里的延迟（0ms 的用例回退到墙上时间）"
    assert summary.tool_span_counts == (1, 1)
    assert summary.workflow_run_counts == (1, 1)
    assert summary.ok is False
    assert summary.exit_code == 3, "退出码 = failed + errors（CLI 契约）"


async def test_latency_comes_from_assertion_context() -> None:
    """`max_latency` 用观察里的 `latency_ms` 判定（不是 Runner 的墙上时间）。"""
    executor = _ScriptedExecutor({"a": CaseObservation(answer="72", latency_ms=49)})
    summary = await _runner(executor).run_suite(
        _Source(EvalSuite(name="lat", cases=(_case("a", CONTAINS_72, MAX_LATENCY_50),)))
    )
    assert summary.results[0].status == "passed"
    assert summary.metrics.p95_latency_ms == 49.0


# ------------------------------------------------- 超时 / 异常收敛


async def test_case_timeout_from_executor_is_recorded_as_error() -> None:
    """执行器自己先收敛成 `RunTimeoutError`（服务层路径：把 timeout 透传下去）。"""
    executor = _ScriptedExecutor({"slow": RunTimeoutError("case exceeded 0.05s")})
    summary = await _runner(executor).run_suite(
        _Source(EvalSuite(name="t", cases=(_case("slow", CONTAINS_72, timeout_seconds=0.05),)))
    )
    result = summary.results[0]
    assert result.status == "error"
    assert result.error_code == "RUN_TIMEOUT"
    assert result.error_message == "case exceeded 0.05s"
    assert result.timed_out is False, "只有 `asyncio.wait_for` 兜底才算 Runner 侧的 timeout"
    assert executor.timeouts == {"slow": 0.05}, "`cases[].timeout_seconds` 要透传给执行器"
    assert summary.exit_code == 1


async def test_case_timeout_fallback_marks_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行器没在超时内返回 → Runner 的 `asyncio.wait_for` 兜底（`timed_out=True`）。"""
    monkeypatch.setattr(runner_module, "CASE_TIMEOUT_SLACK_SECONDS", 0.01)

    async def _hanging(case: EvalCase, agent_id: str, timeout: float) -> CaseObservation:
        await asyncio.sleep(0.3)
        return CaseObservation(answer="72")

    summary = await _runner(_hanging).run_suite(
        _Source(EvalSuite(name="t", cases=(_case("slow", CONTAINS_72, timeout_seconds=0.05),)))
    )
    result = summary.results[0]
    assert result.status == "error"
    assert (result.error_code, result.timed_out) == ("RUN_TIMEOUT", True)
    assert result.error_message == "case exceeded 0.05s"
    assert summary.ok is False


async def test_unexpected_exception_becomes_internal_error() -> None:
    executor = _ScriptedExecutor({"a": RuntimeError("boom")})
    summary = await _runner(executor).run_suite(_Source(EvalSuite(name="x", cases=(_case("a"),))))
    result = summary.results[0]
    assert result.status == "error"
    assert result.error_code == "INTERNAL_ERROR"
    assert result.error_message == "RuntimeError: boom"
    assert result.timed_out is False


async def test_default_case_timeout_is_used_without_override() -> None:
    executor = _ScriptedExecutor({"a": CaseObservation(answer="72")})
    await _runner(executor).run_suite(_Source(EvalSuite(name="t", cases=(_case("a"),))))
    assert executor.timeouts == {"a": runner_module.DEFAULT_CASE_TIMEOUT_SECONDS}


async def test_measured_latency_is_used_when_executor_omits_it() -> None:
    """观察里没给延迟时用 Runner 的墙上时间（这条得用**真实时钟**，其余用例都固定时钟）。"""

    async def _executor(case: EvalCase, agent_id: str, timeout: float) -> CaseObservation:
        await asyncio.sleep(0.05)
        return CaseObservation(answer="72")

    summary = await EvalRunner(execute_case=_executor, agent=AGENT).run_suite(
        _Source(EvalSuite(name="lat", cases=(_case("a", CONTAINS_72),)))
    )
    assert summary.results[0].status == "passed"
    assert summary.results[0].latency_ms >= 20, "观察里没给延迟 → 用 Runner 自己测的墙上时间"
    assert summary.duration_ms >= 20


async def test_agent_mismatch_only_warns() -> None:
    """`--agent` 与执行器装配的 Agent 不一致时只记 warning（报告头写执行器那个）。"""
    executor = _ScriptedExecutor({"a": CaseObservation(answer="72")})
    summary = await _runner(executor).run_suite(
        _Source(EvalSuite(name="m", cases=(_case("a"),))),
        agent_id="ag_other",
    )
    assert summary.agent_id == AGENT.id
    assert summary.ok is True
    assert summary.exit_code == 0


# ------------------------------------------------- 过滤 / 并发 / 顺序


async def test_case_ids_filter_and_unknown_ids() -> None:
    suite = EvalSuite(
        name="filter",
        cases=(_case("a"), _case("b"), _case("disabled", enabled=False)),
    )
    executor = _ScriptedExecutor({"a": CaseObservation(answer="72"), "b": CaseObservation(answer="72")})

    summary = await _runner(executor).run_suite(_Source(suite), case_ids=["b"])
    assert executor.seen == ["b"]
    assert summary.total_cases == 1
    assert summary.suite == "filter", "过滤后仍保留 suite 元信息"
    assert [item.case_id for item in summary.results] == ["b"]

    with pytest.raises(ConfigInvalidError) as unknown:
        await _runner(executor).run_suite(_Source(suite), case_ids=["b", "missing"])
    assert unknown.value.code == "CONFIG_INVALID"
    assert unknown.value.details["case_ids"] == ["missing"], "未知 id 不静默少跑"

    with pytest.raises(ConfigInvalidError) as disabled_case:
        await _runner(executor).run_suite(_Source(suite), case_ids=["disabled"])
    assert disabled_case.value.code == "CONFIG_INVALID", "被 enabled:false 关掉的不算已知 id"


async def test_suite_without_enabled_cases_is_a_config_error() -> None:
    suite = EvalSuite(name="empty", cases=(_case("only", enabled=False),))
    with pytest.raises(ConfigInvalidError) as error:
        await _runner(_ScriptedExecutor({})).run_suite(_Source(suite))
    assert "no enabled cases" in error.value.message
    assert error.value.code == "CONFIG_INVALID"


async def test_concurrency_cap_is_respected() -> None:
    cases = tuple(_case(f"c{index}") for index in range(6))
    executor = _ScriptedExecutor({case.id: CaseObservation(answer="72") for case in cases}, delay=0.02)

    summary = await _runner(executor).run_suite(_Source(EvalSuite(name="cc", cases=cases)), concurrency=2)

    assert executor.max_active == 2, "并发上限生效（SQLite 单写者，不能 6 条一起打）"
    assert summary.concurrency == 2
    assert [item.case_id for item in summary.results] == [case.id for case in cases], "结果按用例集顺序"


async def test_completion_order_does_not_reorder_results() -> None:
    async def _executor(case: EvalCase, agent_id: str, timeout: float) -> CaseObservation:
        await asyncio.sleep(0.05 if case.id == "slow" else 0.0)
        return CaseObservation(answer="72", latency_ms=5)

    done: list[str] = []
    summary = await _runner(_executor).run_suite(
        _Source(EvalSuite(name="order", cases=(_case("slow"), _case("fast")))),
        concurrency=2,
        on_case_done=lambda result: done.append(result.case_id),
    )

    assert done == ["fast", "slow"], "`on_case_done` 按真实完成顺序（进度提示用）"
    assert [item.case_id for item in summary.results] == ["slow", "fast"]
    assert summary.metrics.total_cases == 2


# ------------------------------------------------------------ 报告落盘


async def _report_summary(tmp_path: Path) -> Any:
    """跑一个两用例的小批次（一绿一红）供报告断言。"""
    suite = EvalSuite(
        name="contract",
        description="MVP 契约集",
        source_path=tmp_path / "contract.yaml",
        cases=(_case("ok", CONTAINS_72), _case("bad", CONTAINS_NOPE)),
    )
    executor = _ScriptedExecutor(
        {
            "ok": CaseObservation(
                answer="72",
                latency_ms=25,
                tool_calls=("calculator",),
                tool_spans_total=1,
                tool_spans_succeeded=1,
                trace_id="tr_ok",
            ),
            "bad": CaseObservation(answer="别的答案", latency_ms=80, trace_id="tr_bad"),
        }
    )
    return await _runner(executor).run_suite(_Source(suite), concurrency=1)


async def test_write_report_writes_markdown_and_json(tmp_path: Path) -> None:
    summary = await _report_summary(tmp_path)
    md_path, json_path = write_report(summary, output_dir=tmp_path)

    assert (md_path, json_path) == report_paths(summary, output_dir=tmp_path)
    assert md_path.name == "eval-20261012-093000.md"
    assert json_path.name == "eval-20261012-093000.json"

    body = json.loads(json_path.read_text(encoding="utf-8"))
    assert body == summary_to_dict(summary, report_paths=(md_path, json_path)), "落盘 JSON 与 `--json` 同源"
    assert body["suite"] == "contract"
    assert body["suite_path"] == str(tmp_path / "contract.yaml")
    assert body["agent"] == {"id": "ag_1", "name": "eval-agent", "model": "fake-model", "prompt_version": 3}
    assert body["judge_model"] is None
    assert body["duration_ms"] == 0
    assert body["metrics"]["task_success_rate"] == 0.5
    assert body["metrics"]["tool_success_rate"] == 1.0
    assert [item["case_id"] for item in body["cases"]] == ["ok", "bad"]
    assert body["cases"][1]["trace_id"] == "tr_bad"
    assert body["cases"][1]["status"] == "failed"
    assert body["reports"]["json"].endswith("eval-20261012-093000.json")

    text = md_path.read_text(encoding="utf-8")
    assert text == render_markdown(summary)
    assert text.startswith("# Agent Evaluation Report")
    assert "`ok`" in text and "`bad`" in text


def test_prune_reports_keeps_newest(tmp_path: Path) -> None:
    for index in range(1, 6):
        for suffix in (".md", ".json"):
            (tmp_path / f"eval-20261012-0930{index:02d}{suffix}").write_text("x", encoding="utf-8")
    (tmp_path / "notes.md").write_text("keep me", encoding="utf-8")

    removed = prune_reports(tmp_path, keep=2)

    assert {path.name for path in removed} == {
        f"eval-20261012-0930{index:02d}{suffix}" for index in (1, 2, 3) for suffix in (".md", ".json")
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "eval-20261012-093004.json",
        "eval-20261012-093004.md",
        "eval-20261012-093005.json",
        "eval-20261012-093005.md",
        "notes.md",
    ]
    assert prune_reports(tmp_path, keep=0) == [], "`keep<=0` = 不清理"
    assert len(list(tmp_path.iterdir())) == 5


async def test_write_report_prunes_to_keep(tmp_path: Path) -> None:
    """`write_report(keep=...)` 顺带轮转（4.9.4：`data/reports/` 只留最近 N 份）。"""
    for index in range(1, 4):
        for suffix in (".md", ".json"):
            (tmp_path / f"eval-20261011-0000{index:02d}{suffix}").write_text("old", encoding="utf-8")

    summary = await _report_summary(tmp_path)
    write_report(summary, output_dir=tmp_path, keep=2)

    stems = sorted({path.stem for path in tmp_path.iterdir() if path.name.startswith("eval-")})
    assert stems == ["eval-20261011-000003", "eval-20261012-093000"], "新的那份 + 最近一份旧的"
    assert REPORT_KEEP == 20, "默认保留份数（4.9.4）"
