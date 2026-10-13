"""命令行评测入口（详细设计 4.9.1 / 4.9.4；对应大纲的「`python evaluate.py`」）。

用法（在 `backend/` 下）::

    python -m app.scripts.evaluate --suite contract-suite --agent <id|name>
    python -m app.scripts.evaluate --suite demo-suite --agent research-agent --concurrency 2
    python -m app.scripts.evaluate --suite demo-suite --agent research-agent --cases task-1-calculator
    python -m app.scripts.evaluate --suite demo-suite --agent research-agent --json

约定（4.9.1 末条 + 7.8 任务 4）：

- `--output`：报告目录（默认 `data/reports`，只保留最近 `REPORT_KEEP` 份，4.9.4）；
- `--json`：把结构化结果打到 stdout（**只**打 JSON，方便 CI / 脚本消费），
  内容与落盘的 `.json` 一致，且 `metrics` 与 Markdown 报告逐项相同；
- 退出码 = `failed + errors` 的条数（0 = 全绿，CI 直接判定）；
  配置/环境问题（用例文件写错、库没迁移、Agent 不存在）统一返回 `2`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import check_database, dispose_engine, get_sessionmaker
from app.runtime.eval import (
    DEFAULT_CASE_TIMEOUT_SECONDS,
    CaseResult,
    EvalRunner,
    YamlSuiteSource,
    render_text,
    summary_to_dict,
    write_report,
)
from app.services import eval_service

EXIT_CONFIG_ERROR = 2
"""环境/配置问题（与"用例没过"区分开，见模块 docstring）。"""


def build_parser() -> argparse.ArgumentParser:
    """命令行参数（7.8 任务 4 的 `--suite/--agent/--concurrency/--output/--json` + `--cases/--judge`）。"""
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.evaluate",
        description="跑一个 YAML 评测用例集，产出报告（data/reports/eval-*.md|json）。",
    )
    parser.add_argument("--suite", required=True, help="用例集名（configs/eval/{name}.yaml）或 YAML 路径")
    parser.add_argument("--agent", default=None, help="Agent id 或 name（省略则用 YAML 里的 `agent`）")
    parser.add_argument("--cases", nargs="*", default=None, help="只跑指定用例（默认全部启用用例）")
    parser.add_argument("--concurrency", type=int, default=None, help="用例级并发（默认 EVAL_CONCURRENCY）")
    parser.add_argument("--judge", default=None, help="`llm_judge` 用的模型名（默认同 Agent 模型）")
    parser.add_argument("--output", default=None, help="报告目录（默认 data/reports）")
    parser.add_argument("--json", action="store_true", help="只往 stdout 打结构化 JSON（不打印文本报告）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CASE_TIMEOUT_SECONDS,
        help=f"单用例默认超时秒数（默认 {DEFAULT_CASE_TIMEOUT_SECONDS:g}）",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    """跑一次评测并落盘报告（返回值即进程退出码）。"""
    settings = get_settings()
    ready, detail = await check_database()
    if not ready:
        print(f"database is not ready: {detail}", file=sys.stderr)  # noqa: T201
        print("hint: alembic upgrade head", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG_ERROR

    source = YamlSuiteSource(args.suite)
    suite = source.load()
    concurrency = args.concurrency if args.concurrency is not None else settings.eval_concurrency
    try:
        async with get_sessionmaker()() as session:
            setup = await eval_service.build_setup(
                session,
                settings,
                suite=suite,
                agent_ref=args.agent,
                judge_model=args.judge,
            )
            runner: EvalRunner = eval_service.make_runner(setup, default_timeout_seconds=args.timeout)
            summary = await runner.run_suite(
                source,
                agent_id=setup.agent.id,
                case_ids=args.cases,
                concurrency=concurrency,
                on_case_done=None if args.json else _progress_printer,
            )
    except AppError as exc:
        print(f"eval failed: {exc.code} {exc.message}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG_ERROR

    md_path, json_path = write_report(
        summary,
        output_dir=Path(args.output) if args.output else None,
    )
    if args.json:
        payload = summary_to_dict(summary, report_paths=(md_path, json_path))
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(summary))
        sys.stdout.write(f"\nReport: {md_path}\n")
        sys.stdout.write(f"JSON:   {json_path}\n")
    return summary.exit_code


def _progress_printer(result: CaseResult) -> None:
    """文本模式下每跑完一条用例就打一行进度（长用例集时不用干等）。"""
    print(f"  ... {result.status:<6} {result.case_id} ({result.latency_ms}ms)")  # noqa: T201


async def _main(args: argparse.Namespace) -> int:
    try:
        return await _run(args)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """脚本入口（`__main__` 与单测共用）。"""
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
