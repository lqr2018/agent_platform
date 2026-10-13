"""评测 Runner 端到端（详细设计 7.8 的集成要求 / 4.9.1–4.9.4）。

测试自建"契约用例要跑的 Agent"（`kind=fake` 的 LLM Provider + `calculator` 工具 + 一个 ready 的知识库，
**不依赖 seed 数据、不打网络**，即 `configs/eval/contract-suite.yaml` 头部写的前置条件），然后：

1. `eval_service.run_suite` 跑 `contract-suite.yaml`（FakeLLMProvider）→ 断言 5 条启用用例的结果与
   `task_success_rate` 符合 fixture 预期（7.8 的集成要求）；
2. 机制证据（4.9.4 第一条的"工具被调用 / 引用被注入 / 错误被回填"）：每个用例一条 `runs.kind=eval` +
   一个独立会话；`tool_invocations` 四行（task-4 那行 `failed`）；绑 KB 的用例有 `retriever` span 且
   `output.chunk_ids` 非空（引用确实进了 system 的 `<chunk>` 块）；
3. 指标口径（4.9.2）：`tool_success_rate = 3/4`（task-4 的一次失败 tool span）、
   `workflow_success_rate = null`（Agent 未绑 Workflow）、`enabled: false` 的 `task-6-kb-search` 不进分母；
4. 报告落盘（4.9.4）与 `python -m app.scripts.evaluate` 命令行：`--json` 的结构与报告 JSON 的指标一致、
   退出码 = `failed + errors`、失败用例带 `trace_id`、未启用的用例 id 直接指定报 `CONFIG_INVALID`。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Conversation, ModelProvider, Run, Span, ToolInvocation
from app.runtime.eval import DEFAULT_SUITE_DIR, load_suite, render_text, write_report
from app.runtime.tools.registry import builtin_tool_id
from app.scripts import evaluate
from app.services import eval_service
from tests.helpers import create_agent, create_fake_provider

BACKEND_DIR = Path(__file__).resolve().parents[2]
"""`backend/`（CLI 子进程的 cwd，`python -m app.scripts.evaluate` 靠它找到 `app` 包）。"""

SUITE_PATH = DEFAULT_SUITE_DIR / "contract-suite.yaml"
SUITE_NAME = "contract-suite"
CONVERSATION_PREFIX = f"eval:{SUITE_NAME}:"
"""会话/span 名的前缀（`eval_service.EVAL_CONVERSATION_PREFIX` + 集名）。"""

EMBEDDING_MODEL = "fake-embedding"
CALCULATOR_ID = builtin_tool_id("calculator")
KB_DOC_TEXT = "# 安装\n\n先 clone 仓库，再执行 pip install -e .[dev]，最后跑迁移。\n"
"""KB 语料（与 `test_knowledge_api.py` 同源，只为"索引非空"准备）。"""

ENABLED_CASE_IDS = (
    "task-1-calculator",
    "task-2-kb-prefetch",
    "task-3-equals-normalize",
    "task-4-tool-error-backfill",
    "task-5-plain-answer",
)
"""`contract-suite.yaml` 里 `enabled: true` 的 5 条用例（顺序 = 文件顺序 = 结果顺序）。"""

DISABLED_CASE_ID = "task-6-kb-search"
"""`kb_search` 属迭代 E：先留位并 `enabled: false`，不进分母、也不能用 `--cases` 单独指定。"""


def _cli_env() -> dict[str, str]:
    """把当前测试环境原样传给 CLI 子进程（临时 DB / 数据目录 / `VECTOR_STORE_KIND=memory`）。"""
    env = {str(key): str(value) for key, value in os.environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _stdout_json(text: str) -> dict[str, Any]:
    """取 stdout 里的 JSON 段。

    structlog 默认往 stdout 打日志（`configure_logging` 只在 `create_app` 里调用，CLI 不调用），
    而 `--json` 的 payload 是最后一段、且首行是独立的 `{`（`json.dumps(..., indent=2)`）——
    所以从**最后一个独立 `{` 行**往后切，日志里的 `details={...}` 不会干扰。
    """
    lines = text.splitlines()
    start = len(lines) - 1 - lines[::-1].index("{")
    return json.loads("\n".join(lines[start:]))


async def _wait_document(client: AsyncClient, kb_id: str, doc_id: str, *, timeout: float = 5.0) -> str:
    """轮询 `documents.status` 到终态（4.6.2 的进度判据）。"""
    deadline = time.monotonic() + timeout
    status = "pending"
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{doc_id}")
        status = str(response.json()["data"]["status"])
        if status in {"ready", "failed"}:
            return status
        await asyncio.sleep(0.05)
    return status


async def _eval_runs(session: AsyncSession) -> list[Run]:
    """本批评测落下的 `runs`（`kind=eval`）。"""
    rows = await session.execute(select(Run).where(Run.kind == "eval").order_by(Run.started_at))
    return list(rows.scalars().all())


async def _spans(session: AsyncSession, trace_id: str) -> list[Span]:
    """某条 trace 的全部 span（按 `seq`）。"""
    rows = await session.execute(select(Span).where(Span.trace_id == trace_id).order_by(Span.seq))
    return list(rows.scalars().all())


def _retriever_spans(spans: list[Span]) -> list[Span]:
    return [span for span in spans if span.span_type == "retriever"]


@pytest.fixture(autouse=True)
def _rag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内向量库 + 允许确定性 embedding（SD-11 / 4.1.3）。

    必须 autouse：`app` 夹具会 `create_app()`，配置在那一刻被读定（与 `test_knowledge_api.py` 同因）。
    """
    monkeypatch.setenv("VECTOR_STORE_KIND", "memory")
    monkeypatch.setenv("EMBED_ALLOW_FAKE", "true")
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def embedding_provider(session: AsyncSession) -> ModelProvider:
    """`kind=fake` 的 embedding Provider（4.1.3：只有测试期能直写，API 永远 422）。"""
    provider = ModelProvider(
        name="eval-fake-embedder",
        kind="fake",
        base_url="fake://",
        models=[{"name": EMBEDDING_MODEL}],
        default_params={},
        headers={},
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    return provider


@pytest_asyncio.fixture
async def kb(app_client: AsyncClient, embedding_provider: ModelProvider) -> str:
    """建一个已有 ready 文档的 KB，返回 id。

    `score_threshold=0`：假 embedding 下换任何问句都能命中 —— 契约用例要的是"检索这个机制没坏"，
    不该让相似度抖动决定用例红绿（真实阈值行为由 `test_knowledge_api.py` 覆盖）。
    """
    created = await app_client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "eval-kb",
            "description": "评测 Runner 的固定预检索用例",
            "embedding_provider_id": embedding_provider.id,
            "embedding_model": EMBEDDING_MODEL,
            "splitter": "markdown",
            "chunk_size": 200,
            "chunk_overlap": 40,
            "top_k": 3,
            "score_threshold": 0.0,
        },
    )
    assert created.status_code == 201, created.text
    kb_id = str(created.json()["data"]["id"])
    uploaded = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("install.md", KB_DOC_TEXT.encode("utf-8"), "text/markdown")},
    )
    assert uploaded.status_code == 202, uploaded.text
    doc_id = str(uploaded.json()["data"]["id"])
    assert await _wait_document(app_client, kb_id, doc_id) == "ready"
    return kb_id


@pytest_asyncio.fixture
async def eval_agent(app_client: AsyncClient, session: AsyncSession, kb: str) -> dict[str, Any]:
    """契约用例要跑的 Agent：fake LLM + `calculator` + 绑定 KB（= YAML 头部声明的前置条件）。"""
    provider = await create_fake_provider(session, name="eval-llm-provider")
    agent = await create_agent(
        app_client,
        provider_id=str(provider.id),
        name="eval-agent",
        tool_ids=[CALCULATOR_ID],
        knowledge_base_ids=[kb],
    )
    assert agent["tool_ids"] == [CALCULATOR_ID] and agent["knowledge_base_ids"] == [kb]
    return agent


async def test_contract_suite_runs_green_end_to_end(
    session: AsyncSession,
    eval_agent: dict[str, Any],
    kb: str,
    tmp_path: Path,
) -> None:
    """7.8 集成要求：跑 contract-suite → 用例结果 / 指标 / 落库证据 / 报告全部符合 fixture 预期。"""
    suite = load_suite(SUITE_NAME)
    assert suite.agent is None  # 契约用例不声明 `agent:`：必须显式 `--agent`（YAML 头部约定）
    assert len(suite.cases) == 6 and [case.id for case in suite.enabled_cases] == list(ENABLED_CASE_IDS)

    summary = await eval_service.run_suite(
        session,
        get_settings(),
        suite=suite,
        agent_ref=str(eval_agent["name"]),
        concurrency=2,
    )

    # --- 报告头 = 4.9.1 第 1 步的 Agent 快照 ---
    assert summary.suite == SUITE_NAME and summary.suite_path == SUITE_PATH
    assert summary.total_cases == len(ENABLED_CASE_IDS)  # `enabled: false` 不算进分母（4.9.2）
    assert (summary.agent_id, summary.agent_name) == (str(eval_agent["id"]), "eval-agent")
    assert summary.model_name == "fake-model" and summary.prompt_version == 1
    assert summary.judge_model is None  # contract-suite 里没有 `llm_judge`（4.9.3）
    assert summary.concurrency == 2 and summary.duration_ms >= 0

    # --- 指标（4.9.2）---
    metrics = summary.metrics
    assert (metrics.passed, metrics.failed, metrics.errors) == (5, 0, 0)
    assert metrics.task_success_rate == 1.0 and metrics.error_rate == 0.0
    assert summary.tool_span_counts == (3, 4)  # 一共 4 次工具调用，task-4 的那次失败
    assert metrics.tool_success_rate == 0.75
    assert summary.workflow_run_counts == (0, 0) and metrics.workflow_success_rate is None
    assert metrics.avg_judge_score is None
    assert metrics.avg_tool_calls == 0.8 and metrics.p95_latency_ms > 0
    assert metrics.total_cost_usd == sum((item.cost_usd for item in summary.results), Decimal(0))
    assert summary.ok is True and summary.exit_code == 0

    # --- 逐用例（结果顺序 = YAML 顺序，并发不改变顺序）---
    by_id = {item.case_id: item for item in summary.results}
    assert list(by_id) == list(ENABLED_CASE_IDS) and DISABLED_CASE_ID not in by_id
    assert all(item.status == "passed" for item in summary.results)
    assert all(item.trace_id and item.run_id for item in summary.results)  # DoD：能顺 trace 定位
    assert all(assertion.passed for item in summary.results for assertion in item.assertions)
    assert by_id["task-1-calculator"].tool_calls == ("calculator",)
    assert by_id["task-3-equals-normalize"].answer == "结果：8 / 2 = 4"
    assert by_id["task-5-plain-answer"].tool_calls == ()
    failed_case = by_id["task-4-tool-error-backfill"]
    assert (failed_case.tool_spans_total, failed_case.tool_spans_succeeded) == (1, 0)

    # --- 落库证据：一个用例 = 一条 `kind=eval` 的 Run + 一个独立会话（4.9.1 第 2 步）---
    runs = await _eval_runs(session)
    assert len(runs) == 5 and {run.kind for run in runs} == {"eval"}
    assert {run.status for run in runs} == {"succeeded"}  # 4.9.3：断言与工具失败都不改 Run 状态
    conversations = list(
        (
            await session.execute(select(Conversation).where(Conversation.title.startswith(CONVERSATION_PREFIX)))
        ).scalars()
    )
    assert sorted(item.title for item in conversations) == sorted(
        f"{CONVERSATION_PREFIX}{case_id}" for case_id in ENABLED_CASE_IDS
    )
    assert {run.conversation_id for run in runs} == {item.id for item in conversations}

    # --- "工具被调用" + "错误被回填"（4.9.4）：工具明细四行，task-4 那行 failed ---
    invocations = list((await session.execute(select(ToolInvocation))).scalars().all())
    assert [item.tool_name for item in invocations] == ["calculator"] * 4
    tool_failures = [item for item in invocations if item.status == "failed"]
    assert len(tool_failures) == 1
    assert tool_failures[0].error_code == "TOOL_EXECUTION_FAILED"
    assert tool_failures[0].trace_id == failed_case.trace_id

    # --- "引用被注入"（4.9.4）：绑 KB 的用例在首轮 LLM 之前预检索一次，命中切片进 system ---
    for item in summary.results:
        spans = await _spans(session, str(item.trace_id))
        retriever = _retriever_spans(spans)
        assert len(retriever) == 1  # 每个 Run 只检索一次（4.4.2）
        assert retriever[0].status == "ok"
        assert retriever[0].attributes["strategy"] == "prefetch"
        assert retriever[0].attributes["kb_ids"] == [kb]
        assert retriever[0].attributes["hit_count"] >= 1
        assert retriever[0].output["chunk_ids"]  # 4.6.3：命中切片进了 system 的 `<chunk>` 块
        assert len([span for span in spans if span.span_type == "tool"]) == len(item.tool_calls)

    # --- 报告落盘（4.9.4：`.md` + `.json`，文件名 `eval-<ts>`）---
    md_path, json_path = write_report(summary, output_dir=tmp_path)
    assert md_path.exists() and json_path.exists()
    assert md_path.name.startswith("eval-") and json_path.name == f"{md_path.stem}.json"
    body = json.loads(json_path.read_text(encoding="utf-8"))
    assert body["metrics"] == metrics.as_dict()
    assert body["suite"] == SUITE_NAME and body["agent"]["name"] == "eval-agent"
    assert [case["case_id"] for case in body["cases"]] == list(ENABLED_CASE_IDS)
    assert body["reports"] == {"markdown": str(md_path), "json": str(json_path)}
    markdown = md_path.read_text(encoding="utf-8")
    assert "100.0% (5/5)" in markdown and "75.0%   (3/4)" in markdown
    assert "| `workflow_success_rate` | n/a |" in markdown
    assert "（本批全部通过）" in markdown
    assert render_text(summary).startswith("Agent Evaluation Report")


async def test_cli_json_stdout_matches_report_files(
    eval_agent: dict[str, Any],
    tmp_path: Path,
) -> None:
    """7.8 集成要求：`--json` 的结构与报告文件一致（失败用例带 `trace_id`），退出码 = 失败数。

    用子进程跑真正的入口（`python -m app.scripts.evaluate`），覆盖 argparse / `main()` / 退出码链路；
    环境变量整体继承（临时 SQLite / `DATA_DIR` / `VECTOR_STORE_KIND=memory`）。
    """
    output_dir = tmp_path / "reports"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.scripts.evaluate",
            "--suite",
            SUITE_NAME,
            "--agent",
            str(eval_agent["name"]),
            "--cases",
            "task-1-calculator",
            "task-5-plain-answer",
            "--concurrency",
            "1",
            "--output",
            str(output_dir),
            "--json",
        ],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    payload = _stdout_json(completed.stdout)
    assert "Agent Evaluation Report" not in completed.stdout  # `--json` 只打 JSON（4.9.4）
    assert payload["suite"] == SUITE_NAME
    assert payload["agent"]["name"] == "eval-agent"
    assert payload["metrics"]["total_cases"] == 2 and payload["metrics"]["task_success_rate"] == 1.0
    assert [case["case_id"] for case in payload["cases"]] == ["task-1-calculator", "task-5-plain-answer"]
    assert all(case["trace_id"] for case in payload["cases"])

    json_report = Path(payload["reports"]["json"])
    md_report = Path(payload["reports"]["markdown"])
    assert json_report.parent == output_dir and json_report.name.startswith("eval-")
    assert md_report.exists() and json_report.exists()
    assert json.loads(json_report.read_text(encoding="utf-8")) == payload  # stdout 与落盘同源


async def test_cli_failing_case_exits_with_failure_count(
    eval_agent: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """4.9.4 DoD：用例没过时退出码 = `failed + errors`，且 `--json` 里带 `trace_id` 可供定位。"""
    suite_path = tmp_path / "breaking-suite.yaml"
    suite_path.write_text(
        "name: breaking-suite\n"
        "agent: eval-agent\n"
        "cases:\n"
        "  - id: task-broken\n"
        "    input: 你好\n"
        "    expected:\n"
        "      - type: contains\n"
        "        value: 不可能出现的字符串\n",
        encoding="utf-8",
    )
    args = evaluate.build_parser().parse_args(
        ["--suite", str(suite_path), "--output", str(tmp_path / "reports"), "--json"]
    )

    assert await evaluate._run(args) == 1

    payload = _stdout_json(capsys.readouterr().out)
    assert payload["metrics"]["failed"] == 1 and payload["metrics"]["task_success_rate"] == 0.0
    case = payload["cases"][0]
    assert case["status"] == "failed" and case["trace_id"]
    assert [item["type"] for item in case["assertions"]] == ["contains"]
    assert case["assertions"][0]["passed"] is False


async def test_cli_disabled_case_id_is_config_error(
    eval_agent: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`enabled: false` 的占位用例（`kb_search` 属迭代 E）不能被 `--cases` 单独跑：`CONFIG_INVALID` + 2。"""
    args = evaluate.build_parser().parse_args(
        [
            "--suite",
            SUITE_NAME,
            "--agent",
            str(eval_agent["name"]),
            "--cases",
            DISABLED_CASE_ID,
            "--output",
            str(tmp_path),
        ]
    )

    assert await evaluate._run(args) == evaluate.EXIT_CONFIG_ERROR

    captured = capsys.readouterr()
    assert "CONFIG_INVALID" in captured.err and DISABLED_CASE_ID in captured.err
    assert list(tmp_path.glob("eval-*")) == []  # 配置错误不落半份报告
