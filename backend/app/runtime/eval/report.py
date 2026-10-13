"""报告渲染与落盘（详细设计 4.9.2 的模板 + 4.9.4 的 `data/reports/` 约定）。

- `render_text()`：**stdout** 上那份（模板逐字对齐 4.9.2，包含 Cases 段与失败原因）；
- `render_markdown()`：`data/reports/eval-{ts}.md`（README 的"Evaluation Results"直接引用它）；
- `write_report()`：一次落 `.md` + `.json` 两份，并只保留最近 `REPORT_KEEP` 份
  （4.9.4：常量写死在这里，**不新增环境变量**；不做 DB 清理/归档，SD-12 / SD-18）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import Settings, get_settings
from app.core.enums import EvalOutcome
from app.core.logging import get_logger
from app.runtime.eval.assertions import assertion_summary
from app.runtime.eval.metrics import CaseResult, judge_distribution

if TYPE_CHECKING:
    from app.runtime.eval.runner import EvalRunSummary

logger = get_logger(__name__)

REPORT_KEEP = 20
"""4.9.4：`data/reports/` 只保留最近 20 份（不做自动清理 DB，SD-12）。"""

REPORT_PREFIX = "eval-"
REPORT_SUFFIXES = (".md", ".json")
CASE_NAME_WIDTH = 30
NO_VALUE = "—"


def summary_to_dict(summary: EvalRunSummary, *, report_paths: tuple[Path, Path] | None = None) -> dict[str, Any]:
    """报告 JSON / `--json` 的结构（4.9.4：两者指标必须一致）。"""
    body: dict[str, Any] = {
        "suite": summary.suite,
        "suite_path": str(summary.suite_path) if summary.suite_path else None,
        "agent": {
            "id": summary.agent_id,
            "name": summary.agent_name,
            "model": summary.model_name,
            "prompt_version": summary.prompt_version,
        },
        "judge_model": summary.judge_model,
        "concurrency": summary.concurrency,
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat(),
        "duration_ms": summary.duration_ms,
        "metrics": summary.metrics.as_dict(),
        "cases": [item.as_dict() for item in summary.results],
        "reports": {
            "markdown": str(report_paths[0]) if report_paths else None,
            "json": str(report_paths[1]) if report_paths else None,
        },
    }
    return body


def write_report(
    summary: EvalRunSummary,
    *,
    output_dir: Path | None = None,
    keep: int = REPORT_KEEP,
) -> tuple[Path, Path]:
    """落 `.md` + `.json`（4.9.1 第 6 步），并只保留最近 `keep` 份。"""
    md_path, json_path = report_paths(summary, output_dir=output_dir)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    body = summary_to_dict(summary, report_paths=(md_path, json_path))
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    json_path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prune_reports(md_path.parent, keep=keep)
    logger.info("eval.report_written", markdown=str(md_path), json=str(json_path))
    return md_path, json_path


def prune_reports(directory: Path, *, keep: int = REPORT_KEEP) -> list[Path]:
    """只保留最近 `keep` 份报告（按文件名里的时间戳排序；`keep<=0` 视为不清理）。"""
    if keep <= 0 or not directory.exists():
        return []
    stems = sorted({path.stem for path in directory.glob(f"{REPORT_PREFIX}*") if path.suffix in REPORT_SUFFIXES})
    stale = stems[:-keep] if len(stems) > keep else []
    removed: list[Path] = []
    for stem in stale:
        for suffix in REPORT_SUFFIXES:
            target = directory / f"{stem}{suffix}"
            if target.exists():
                target.unlink()
                removed.append(target)
    if removed:
        logger.info("eval.reports_pruned", count=len(removed), keep=keep)
    return removed


def _case_lines(results: Sequence[CaseResult]) -> list[str]:
    """模板里的 `Cases:` 段（成功用例只留指标，失败用例附第一条失败断言）。"""
    lines: list[str] = []
    for item in results:
        score = f"{item.judge_score:.2f}" if item.judge_score is not None else NO_VALUE
        line = (
            f"  {_outcome_icon(item)} {item.case_id:<{CASE_NAME_WIDTH}}"
            f"{_seconds(item.latency_ms):>8}{_tool_label(item):>9}{score:>7}"
        )
        summary_line = _first_failure(item)
        if summary_line:
            line += f"   {summary_line}"
        lines.append(line)
    return lines


def _failure_lines(results: Sequence[CaseResult]) -> list[str]:
    """失败断言明细（DoD：失败用例必须能顺着 `trace_id` 定位）。"""
    lines: list[str] = []
    for item in results:
        failed = [item_ for item_ in item.assertions if not item_.passed]
        if not failed:
            continue
        lines.append(f"  {item.case_id} (trace_id={item.trace_id or NO_VALUE})")
        lines.extend(f"    - {assertion_summary(item_)}  [{item_.detail}]" for item_ in failed)
    return lines


def _first_failure(item: CaseResult) -> str:
    """第一条失败断言的一行摘要（对齐模板的 `expected tool_sequence [...]`）。"""
    for assertion in item.assertions:
        if not assertion.passed:
            return assertion_summary(assertion)
    if item.error_message:
        return f"{item.error_code or 'ERROR'}: {item.error_message}"
    return ""


def _agent_label(summary: EvalRunSummary) -> str:
    version = f"(prompt v{summary.prompt_version})" if summary.prompt_version else ""
    return f"{summary.agent_name} {version}".strip()


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ratio(value: float | None, counts: tuple[int, int]) -> str:
    succeeded, total = counts
    if value is None or total == 0:
        return "n/a"
    return f"{_percent(value)}   ({succeeded}/{total})"


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / 1000:.2f}s"


def _money(amount: Decimal) -> str:
    return f"${amount:.4f}"


def _outcome_icon(item: CaseResult) -> str:
    icons = {
        str(EvalOutcome.PASSED): "✅",
        str(EvalOutcome.FAILED): "❌",
        str(EvalOutcome.ERROR): "⚠️",
    }
    return icons.get(item.status, "?")


def _tool_label(item: CaseResult) -> str:
    count = len(item.tool_calls)
    return f"{count} tool" if count == 1 else f"{count} tools"


def report_filename(started_at: datetime) -> str:
    """报告文件名（`eval-YYYYmmdd-HHMMSS`；`ts` 只作排序键）。"""
    return f"{REPORT_PREFIX}{started_at.strftime('%Y%m%d-%H%M%S')}"


def report_paths(summary: EvalRunSummary, *, output_dir: Path | None = None) -> tuple[Path, Path]:
    """`.md` / `.json` 的目标路径（`--output` 传目录时用它）。"""
    directory = Path(output_dir) if output_dir is not None else reports_dir()
    stem = report_filename(summary.started_at)
    return directory / f"{stem}.md", directory / f"{stem}.json"


def reports_dir(settings: Settings | None = None) -> Path:
    """`data/reports`（附录 C 的运行时目录之一）。"""
    return (settings or get_settings()).data_path / "reports"


def render_text(summary: EvalRunSummary) -> str:
    """4.9.2 的报告模板（stdout 与 Markdown 的第一段共用同一份数据）。"""
    metrics = summary.metrics
    lines = [
        "Agent Evaluation Report",
        "=======================",
        f"Suite:       {summary.suite:<16}Agent:  {_agent_label(summary)}",
        f"Model:       {summary.model_name:<16}Cases:  {metrics.total_cases}/{summary.total_cases} (enabled)",
        "-" * 60,
        f"Success Rate:           {_percent(metrics.task_success_rate)}   ({metrics.passed}/{metrics.total_cases})",
        f"Tool Success Rate:      {_ratio(metrics.tool_success_rate, summary.tool_span_counts)}",
        f"Workflow Success Rate:  {_ratio(metrics.workflow_success_rate, summary.workflow_run_counts)}",
        f"Average Latency:        {_seconds(metrics.avg_latency_ms)}",
        f"P95 Latency:            {_seconds(metrics.p95_latency_ms)}",
        f"Average Tool Calls:     {metrics.avg_tool_calls:.2f}",
        f"Average Token Usage:    {metrics.avg_total_tokens:.0f}",
        f"Total Cost (est.):      {_money(metrics.total_cost_usd)}",
        f"Error Rate:             {_percent(metrics.error_rate)}",
        "-" * 60,
        "Cases:",
    ]
    lines.extend(_case_lines(summary.results))
    failures = _failure_lines(summary.results)
    if failures:
        lines.append("-" * 60)
        lines.append("Failed assertions:")
        lines.extend(failures)
    return "\n".join(lines) + "\n"


def render_markdown(summary: EvalRunSummary) -> str:
    """Markdown 版报告（落盘用；表格化，便于 README / PR 里贴）。"""
    metrics = summary.metrics
    lines = [
        "# Agent Evaluation Report",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| Suite | `{summary.suite}` |",
        f"| Agent | {_agent_label(summary)} |",
        f"| Model | `{summary.model_name}` |",
        f"| Judge | `{summary.judge_model}` |" if summary.judge_model else "| Judge | 未使用（无 `llm_judge` 断言） |",
        f"| Cases | {metrics.total_cases}/{summary.total_cases} (enabled) |",
        f"| Started | {summary.started_at.isoformat(timespec='seconds')} |",
        f"| Duration | {_seconds(summary.duration_ms)} (concurrency={summary.concurrency}) |",
        "",
        "## Metrics",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| `task_success_rate` | {_percent(metrics.task_success_rate)} ({metrics.passed}/{metrics.total_cases}) |",
        f"| `tool_success_rate` | {_ratio(metrics.tool_success_rate, summary.tool_span_counts)} |",
        f"| `workflow_success_rate` | {_ratio(metrics.workflow_success_rate, summary.workflow_run_counts)} |",
        f"| `avg_latency_ms` | {metrics.avg_latency_ms:.1f} |",
        f"| `p95_latency_ms` | {metrics.p95_latency_ms:.1f} |",
        f"| `avg_tool_calls` | {metrics.avg_tool_calls:.2f} |",
        f"| `avg_total_tokens` | {metrics.avg_total_tokens:.1f} |",
        f"| `total_cost_usd` | {_money(metrics.total_cost_usd)} |",
        f"| `error_rate` | {_percent(metrics.error_rate)} |",
        f"| `avg_judge_score` | {metrics.avg_judge_score if metrics.avg_judge_score is not None else NO_VALUE} |",
        "",
        "## Cases",
        "",
        "| 结果 / 用例 | 延迟 | 工具 | 断言 | judge | trace |",
        "|---|---|---|---|---|---|",
    ]
    for item in summary.results:
        lines.append(
            f"| {_outcome_icon(item)} `{item.case_id}` | {_seconds(item.latency_ms)} | {_tool_label(item)} "
            f"| {item.assertions_passed}/{len(item.assertions)} | "
            f"{item.judge_score if item.judge_score is not None else NO_VALUE} "
            f"| `{item.trace_id or NO_VALUE}` |"
        )
    lines.extend(["", "## Failed assertions", ""])
    failures = _failure_lines(summary.results)
    lines.extend(failures if failures else ["（本批全部通过）"])
    distribution = judge_distribution(summary.results)
    if distribution:
        lines.extend(
            [
                "",
                "## Judge scores",
                "",
                f"`llm_judge` 分数分布（4.9.4：仅供参考，确定性断言优先）："
                f"{', '.join(f'{score:.2f}' for score in distribution)}",
            ]
        )
    lines.append("")
    return "\n".join(lines)
