"""三个 Demo Agent 的端到端契约测试（详细设计 8.5 的验收表 / 7.9 的测试要求）。

`pytest tests/e2e/test_demo_agents.py` 就是 8.5 表格里的那一行 —— **三个 Agent 各跑通一条链路**，
且证据来自 HTTP + 落库，而不是服务层白盒调用：

| Demo | 链路 | 断言落点 |
|---|---|---|
| Research Agent（8.2） | Workflow（4.4.4 的 Chat 内联） | `workflow.node.*` 走完整张图 + `runs.kind=workflow` |
| Data Analysis Agent（8.3） | Tool Calling | Chat SSE 的 `tool.call.*` + `tool_invocations` 行 |
| Knowledge Assistant（8.4） | RAG 引用 | Chat SSE 的 `retrieval.completed` + `retriever` span（4.4.2 模式 a） |

环境与 `tests/integration/test_seed_demo.py` 一致：临时 SQLite（9.1）+ `kind=fake` Provider（4.1.3）+
进程内向量库（SD-11）。装载**只用脚本自己的入口**（`seed_demo.seed`）：YAML 与 API 契约一旦漂移，
这里会先红。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Agent, KnowledgeBase, Run, Span, ToolInvocation, Workflow
from app.runtime.tools.registry import builtin_tool_id
from app.scripts import seed_demo
from app.services import agent_config, task_runner, workflow_config
from tests.helpers import FAKE_MODEL, collect_sse, create_conversation, create_fake_provider

DOCS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "docs"
RESEARCH_PROMPT = "调研一下国内主流向量数据库的差异，并给出选型建议"
CALCULATE_PROMPT = "帮我计算 (120+30)*4"
RAG_QUESTION = "部署方式"
"""RAG 问句取 8.4 体验问句的短版（"我们产品的部署方式有哪些？"）。

`FakeEmbedder` 是确定性替身（单字 CJK 词元 + 长 chunk），分数与真实模型不可比：完整问句只有 0.26
（低于默认 `score_threshold=0.3`），短版 0.45 稳定命中 —— 这里要证的是**链路**，不是分数。
"""


@pytest.fixture(autouse=True)
def _rag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内向量库 + 允许确定性 embedding（SD-11 / 4.1.3）；必须在读 `Settings` 之前生效。"""
    monkeypatch.setenv("VECTOR_STORE_KIND", "memory")
    monkeypatch.setenv("EMBED_ALLOW_FAKE", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_task_runner() -> None:
    """摄取队列是进程级单例；换一个干净的 runner，避免复用别的测试 `stop()` 过的实例。"""
    task_runner.reset_task_runner()


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（与 `app_client` 指向同一个临时库）。"""
    return session


async def _seed_demo(db_session: AsyncSession, *, docs: bool = False) -> dict[str, Agent]:
    """经 `seed_demo.seed` 装载三个 Demo Agent，返回 `name → Agent`（按 Agent 自己的 name 索引）。"""
    provider = await create_fake_provider(db_session, name="demo-provider")
    await seed_demo.seed(
        db_session,
        get_settings(),
        config_dir=agent_config.CONFIG_DIR,
        workflow_dir=workflow_config.CONFIG_DIR,
        provider_ref=provider.name,
        model_name=FAKE_MODEL,
        embedding_provider_ref=provider.name,
        embedding_model=FAKE_MODEL,
        docs_dir=DOCS_DIR if docs else None,
    )
    rows = (await db_session.execute(select(Agent))).scalars().all()
    return {row.name: row for row in rows}


async def _post_message(client: AsyncClient, conversation_id: str, content: str) -> list[tuple[str, dict]]:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content, "stream": True},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return await collect_sse(response.text)


@pytest.mark.asyncio
async def test_research_agent_workflow_chain(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """8.2 的 Demo 1：绑了 `research-flow` 的 Agent 在 Chat 里被一条消息驱动整张图（4.4.4 的 Chat 内联）。"""
    agents = await _seed_demo(db_session)
    research = agents["Research Agent"]
    workflow = (await db_session.execute(select(Workflow).where(Workflow.name == "research-flow"))).scalar_one()
    assert research.workflow_id == workflow.id  # 图是写库时绑上的（8.1）

    conversation_id = await create_conversation(app_client, agent_id=str(research.id))
    events = await _post_message(app_client, conversation_id, RESEARCH_PROMPT)
    names = [name for name, _ in events]
    payloads = dict(events)

    assert names[0] == "run.started" and names[-1] == "done"
    assert payloads["run.completed"]["status"] == "succeeded"

    # 事件 13/14（3.4）：整张图被走到，且两条 agent 节点（planner / writer）都成功
    visited = [payload["node_id"] for name, payload in events if name == "workflow.node.completed"]
    assert {"start", "planner", "search", "router", "reader", "writer", "end"} <= set(visited)
    assert all(
        payload["status"] == "succeeded"
        for name, payload in events
        if name == "workflow.node.completed" and payload["node_id"] in {"planner", "writer"}
    )

    # `runs.kind=workflow` + `runs.workflow_run_id`：Trace 树 run → workflow → node → agent 天然串起来
    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert (run.kind, run.status) == ("workflow", "succeeded")
    assert run.workflow_run_id


@pytest.mark.asyncio
async def test_data_analysis_agent_tool_chain(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """8.3 的 Demo 2：不绑 Workflow 也能跑 Agent 循环，且工具调用全程留痕（Tool Calling 链路）。"""
    agents = await _seed_demo(db_session)
    data_agent = agents["Data Analysis Agent"]
    assert data_agent.workflow_id is None  # 证明"没有 Workflow 的平台也照样能跑"
    # 危险工具**挂在配置里**但默认不可用（8.3 的开关式安全设计）：
    # 工具权限判定本身由 `tests/unit/test_tool_permissions.py` 覆盖，这里钉住"默认关闭"这条事实
    assert builtin_tool_id("python_execute") in data_agent.tool_ids
    assert get_settings().python_execute_enabled is False

    conversation_id = await create_conversation(app_client, agent_id=str(data_agent.id))
    events = await _post_message(app_client, conversation_id, CALCULATE_PROMPT)
    names = [name for name, _ in events]
    payloads = dict(events)

    assert names[0] == "run.started" and names[-1] == "done"
    assert "tool.call.started" in names and "tool.call.completed" in names
    assert payloads["tool.call.started"]["tool_name"] == "calculator"
    assert payloads["run.completed"]["tool_call_count"] == 1

    invocation = (
        await db_session.execute(select(ToolInvocation).where(ToolInvocation.tool_name == "calculator"))
    ).scalar_one()
    assert (invocation.status, invocation.permission_decision) == ("succeeded", "allow")

    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert run.status == "succeeded" and run.tool_call_count == 1


@pytest.mark.asyncio
async def test_knowledge_assistant_rag_chain(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """8.4 的 Demo 3：只依据知识库回答 —— 预检索（4.4.2 模式 a）命中并交给 runtime（RAG 链路）。"""
    agents = await _seed_demo(db_session, docs=True)
    assistant = agents["Knowledge Assistant"]
    kb = (await db_session.execute(select(KnowledgeBase).where(KnowledgeBase.name == "product-docs"))).scalar_one()
    assert assistant.knowledge_base_ids == [kb.id] and assistant.workflow_id is None

    conversation_id = await create_conversation(app_client, agent_id=str(assistant.id))
    events = await _post_message(app_client, conversation_id, RAG_QUESTION)
    names = [name for name, _ in events]

    assert names[0] == "run.started" and names[-1] == "done"
    assert "retrieval.completed" in names
    payload = dict(events)["retrieval.completed"]
    assert payload["kb_ids"] == [kb.id]
    assert payload["query"] == RAG_QUESTION
    assert payload["hit_count"] >= 1  # 语料由 `--docs` 灌入（8.4 的 `tests/fixtures/docs`）

    run = (await db_session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert run.status == "succeeded"
    spans = list((await db_session.execute(select(Span).where(Span.trace_id == run.trace_id))).scalars().all())
    retriever_spans = [span for span in spans if span.span_type == "retriever"]
    assert len(retriever_spans) == 1  # 每个 Run 只检索一次
    assert retriever_spans[0].output["chunk_ids"]  # 命中切片确实进了 `retrieved_context`
    assert retriever_spans[0].attributes["strategy"] == "prefetch"
