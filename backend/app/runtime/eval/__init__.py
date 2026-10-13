"""评测运行时（详细设计 4.9 / 7.8；SD-18：MVP = 脚本 + 指标口径，四张 `eval_*` 表属 Backlog）。

模块分工（Backlog 迭代 D 的 `DbSuiteSource` 只需替换 `suite_source`）：

- `suite_source.py`：用例集（YAML → `EvalSuite`）+ `SuiteSource` Protocol；
- `assertions.py`：7 类断言（4.9.3），全部执行、不短路；
- `judges.py`：`llm_judge` 的 judge（严格 JSON、`temperature=0`、成本计入）；
- `metrics.py`：4.9.2 的指标口径（报告与前端 `/stats` 共用字段名）；
- `report.py`：4.9.2 的报告模板 + `data/reports/` 落盘与轮转（`REPORT_KEEP`）；
- `runner.py`：`EvalRunner` + `EvalRunSummary`（编排、并发限流、单 case 超时）。
"""

from __future__ import annotations

from app.runtime.eval.assertions import (
    AssertionContext,
    AssertionResult,
    assertion_summary,
    evaluate_assertion,
    evaluate_assertions,
    failed_assertions,
)
from app.runtime.eval.judges import Judge, JudgeResult, LLMJudge, parse_judge_output
from app.runtime.eval.metrics import (
    CaseResult,
    EvalMetrics,
    compute_metrics,
    judge_distribution,
    percentile,
)
from app.runtime.eval.report import (
    REPORT_KEEP,
    prune_reports,
    render_markdown,
    render_text,
    report_filename,
    report_paths,
    reports_dir,
    summary_to_dict,
    write_report,
)
from app.runtime.eval.runner import (
    DEFAULT_CASE_TIMEOUT_SECONDS,
    AgentSnapshot,
    CaseExecutor,
    CaseObservation,
    EvalRunner,
    EvalRunSummary,
)
from app.runtime.eval.suite_source import (
    DEFAULT_SUITE_DIR,
    EvalCase,
    EvalExpectation,
    EvalSuite,
    SuiteSource,
    YamlSuiteSource,
    load_suite,
    select_cases,
    with_cases,
)

__all__ = [
    "DEFAULT_CASE_TIMEOUT_SECONDS",
    "DEFAULT_SUITE_DIR",
    "REPORT_KEEP",
    "AgentSnapshot",
    "AssertionContext",
    "AssertionResult",
    "CaseExecutor",
    "CaseObservation",
    "CaseResult",
    "EvalCase",
    "EvalExpectation",
    "EvalMetrics",
    "EvalRunSummary",
    "EvalRunner",
    "EvalSuite",
    "Judge",
    "JudgeResult",
    "LLMJudge",
    "SuiteSource",
    "YamlSuiteSource",
    "assertion_summary",
    "compute_metrics",
    "evaluate_assertion",
    "evaluate_assertions",
    "failed_assertions",
    "judge_distribution",
    "load_suite",
    "parse_judge_output",
    "percentile",
    "prune_reports",
    "render_markdown",
    "render_text",
    "report_filename",
    "report_paths",
    "reports_dir",
    "select_cases",
    "summary_to_dict",
    "with_cases",
    "write_report",
]
