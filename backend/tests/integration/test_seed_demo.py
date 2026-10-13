"""`seed_demo` 的集成测试（详细设计 7.9 / 8.1 / 8.5）。

跑的是**脚本自己的函数**（`app.scripts.seed_demo.seed`），环境是临时 SQLite（9.1）+
`kind=fake` 的 Provider（4.1.3）+ 进程内向量库（SD-11），因此覆盖了"配置驱动"最容易出错的几件事：

1. 三份 YAML → 库里的三行 Agent：`tools` / `knowledge_bases` / `workflow` 的 **name 引用被解析成真实 id**；
2. **幂等**（7.9 的 DoD）：连跑两次不新增行、不涨 `workflow.version`、不落多余的 prompt 快照；
3. `--docs` 灌语料：第二次**复用**同 checksum 的文档，切片数与 embedding 都不翻倍；
4. 写错的引用**当面报错**（`ConfigInvalidError`，也就是脚本的退出码 2）：工具名不存在、
   知识库 id 不存在 —— 而不是静默忽略、留下一条"看起来能用"的配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConfigInvalidError
from app.db.models import (
    Agent,
    AgentPromptVersion,
    Chunk,
    Document,
    KnowledgeBase,
    ModelProvider,
    Workflow,
)
from app.runtime.tools.registry import builtin_tool_id
from app.scripts import seed_demo
from app.services import agent_config, task_runner, workflow_config
from tests.helpers import FAKE_MODEL, create_fake_provider

CONFIG_DIR = agent_config.CONFIG_DIR
WORKFLOW_DIR = workflow_config.CONFIG_DIR
DOCS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "docs"
AGENT_NAMES = ["Data Analysis Agent", "Knowledge Assistant", "Research Agent"]
SHIPPED_WORKFLOWS = ["created:kb-qa-flow", "created:research-flow"]

BROKEN_AGENT = """
version: 1
agent:
  name: {name}
  model: {{ provider: demo-provider, name: {model} }}
{tail}
"""


@pytest.fixture(autouse=True)
def _rag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内向量库 + 允许确定性 embedding（SD-11 / 4.1.3）；必须在读 `Settings` 之前生效。"""
    monkeypatch.setenv("VECTOR_STORE_KIND", "memory")
    monkeypatch.setenv("EMBED_ALLOW_FAKE", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_task_runner() -> None:
    """摄取队列是进程级单例，而 `app` 的 lifespan 会在 shutdown 时 `stop()` 它。

    本模块只用 `session`（不起 app），因此显式换一个**新的** runner，避免拿到
    上一个测试遗留的 `_accepting=False` 实例（`kb_service.create_document` 会拒绝入队）。
    """
    task_runner.reset_task_runner()


@pytest_asyncio.fixture
async def provider(session: AsyncSession) -> ModelProvider:
    return await create_fake_provider(session, name="demo-provider")


async def _seed(session: AsyncSession, provider: ModelProvider, **overrides: Any) -> seed_demo.SeedReport:
    kwargs: dict[str, Any] = {
        "config_dir": CONFIG_DIR,
        "workflow_dir": WORKFLOW_DIR,
        "provider_ref": provider.name,
        "model_name": FAKE_MODEL,
        "embedding_provider_ref": provider.name,
        "embedding_model": FAKE_MODEL,
    }
    kwargs.update(overrides)
    return await seed_demo.seed(session, get_settings(), **kwargs)


async def _agents(session: AsyncSession) -> dict[str, Agent]:
    rows = (await session.execute(select(Agent))).scalars().all()
    return {row.name: row for row in rows}


async def _one_workflow(session: AsyncSession, name: str) -> Workflow:
    return (await session.execute(select(Workflow).where(Workflow.name == name))).scalar_one()


def _write_agent(directory: Path, filename: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_seed_creates_three_demo_agents_with_resolved_refs(
    session: AsyncSession,
    provider: ModelProvider,
) -> None:
    """8.1 的装载口径：YAML 里的 name 引用在写库时解析成真实 id。"""
    report = await _seed(session, provider)

    assert report.agents == [f"created:{name}" for name in AGENT_NAMES]
    assert report.knowledge_bases == ["created:product-docs"]
    assert sorted(report.workflows) == SHIPPED_WORKFLOWS

    agents = await _agents(session)
    kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.name == "product-docs"))).scalar_one()
    research = agents["Research Agent"]

    assert research.model_provider_id == provider.id and research.model_name == FAKE_MODEL
    assert research.tool_ids == [builtin_tool_id(name) for name in ("web_search", "calculator", "file_read")]
    assert research.knowledge_base_ids == [kb.id]
    assert research.is_template is True and research.tags == ["demo", "research"]
    assert research.system_prompt.startswith("你是一名严谨的研究员")

    # 三类 Demo 的差别只在配置：研究型绑图 / 数据型不绑图 / 知识库型绑 KB
    workflow = await _one_workflow(session, "research-flow")
    assert research.workflow_id == workflow.id
    assert agents["Data Analysis Agent"].workflow_id is None
    assert agents["Knowledge Assistant"].workflow_id is None
    assert agents["Knowledge Assistant"].knowledge_base_ids == [kb.id]

    # 图里的 `agent_id` / `kb_id` 已被替换成真实 id（再发布）
    assert workflow.status == "published" and workflow.version == 1
    nodes = {node["id"]: node for node in workflow.definition["nodes"]}
    assistant = agents["Knowledge Assistant"]
    # `planner` / `writer` 引用的是**未绑 Workflow** 的 Knowledge Assistant：图内 agent 节点不得
    # 引用绑了图的 Agent（4.4.4 的 `NESTED_WORKFLOW_NOT_SUPPORTED`），否则 Chat 内联会无限展开
    assert nodes["planner"]["agent_id"] == assistant.id
    assert nodes["writer"]["agent_id"] == assistant.id
    assert nodes["planner"]["agent_id"] != research.id
    assert nodes["reader"]["kb_id"] == kb.id

    # 默认路径不碰文档（`--docs` 才灌语料）
    assert report.documents == []


@pytest.mark.asyncio
async def test_seed_is_idempotent(session: AsyncSession, provider: ModelProvider) -> None:
    """7.9 的 DoD：`seed_demo` 可反复执行，第二次不留痕（尤其不涨版本）。"""
    await _seed(session, provider)
    before = await _one_workflow(session, "research-flow")
    before_definition = dict(before.definition)

    report = await _seed(session, provider)

    assert report.knowledge_bases == []
    assert all(item.startswith("updated:") for item in report.agents)
    assert report.workflows == ["unchanged:kb-qa-flow", "unchanged:research-flow"]

    assert await session.scalar(select(func.count(Agent.id))) == 3
    assert await session.scalar(select(func.count(KnowledgeBase.id))) == 1
    assert await session.scalar(select(func.count(Workflow.id))) == 2
    assert await session.scalar(select(func.count(AgentPromptVersion.id))) == 0  # prompt 没变 → 不落快照

    after = await _one_workflow(session, "research-flow")
    assert (after.version, after.status) == (1, "published")
    assert dict(after.definition) == before_definition


@pytest.mark.asyncio
async def test_seed_docs_ingest_then_reuse(session: AsyncSession, provider: ModelProvider) -> None:
    """`--docs`：第一次真摄取，第二次复用同 checksum（切片数与 embedding 都不翻倍）。"""
    first = await _seed(session, provider, docs_dir=DOCS_DIR)
    assert first.documents == ["ready:deployment.md", "ready:platform-overview.md"]
    chunks_after_first = await session.scalar(select(func.count(Chunk.id)))
    assert chunks_after_first and chunks_after_first > 0

    second = await _seed(session, provider, docs_dir=DOCS_DIR)
    assert second.documents == ["reused:deployment.md", "reused:platform-overview.md"]

    assert await session.scalar(select(func.count(Chunk.id))) == chunks_after_first
    # 两次上传各留一行（第二次那行直接 `ready`，切片由 `_find_reusable_document` 指过来）
    assert await session.scalar(select(func.count(Document.id))) == 4


@pytest.mark.asyncio
async def test_seed_rejects_docs_directory_missing(session: AsyncSession, provider: ModelProvider) -> None:
    with pytest.raises(ConfigInvalidError, match="Docs directory not found"):
        await _seed(session, provider, docs_dir=DOCS_DIR / "does-not-exist")


@pytest.mark.asyncio
async def test_seed_rejects_unknown_tool_name(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
) -> None:
    """工具名写错要当面报错（退出码 2），而不是被静默丢掉。"""
    config_dir = tmp_path / "agents"
    _write_agent(
        config_dir,
        "broken-tool.yaml",
        BROKEN_AGENT.format(name="Broken Agent", model=FAKE_MODEL, tail='  tools: ["not_a_tool"]'),
    )
    with pytest.raises(ConfigInvalidError, match="unknown tool"):
        await _seed(session, provider, config_dir=config_dir)


@pytest.mark.asyncio
async def test_seed_rejects_unknown_knowledge_base_id(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
) -> None:
    """`knowledge_bases` 写了一个 26 位的 id 但库里没有 → 报错（不新建一个名为该 id 的 KB）。"""
    config_dir = tmp_path / "agents"
    _write_agent(
        config_dir,
        "broken-kb.yaml",
        BROKEN_AGENT.format(
            name="Broken Agent",
            model=FAKE_MODEL,
            tail='  tools: ["calculator"]\n  knowledge_bases: ["01JZZZZZZZZZZZZZZZZZZZZZZZ"]',
        ),
    )
    with pytest.raises(ConfigInvalidError, match="Knowledge base id not found"):
        await _seed(session, provider, config_dir=config_dir)


@pytest.mark.asyncio
async def test_seed_reset_recreates_agents_and_workflows(session: AsyncSession, provider: ModelProvider) -> None:
    """`--reset` 的语义：先把同名 Agent / Workflow 删掉，于是这次又是 `created`。"""
    await _seed(session, provider)

    report = await _seed(session, provider, reset=True)

    assert report.agents == [f"created:{name}" for name in AGENT_NAMES]
    assert sorted(report.workflows) == SHIPPED_WORKFLOWS
    assert await session.scalar(select(func.count(Agent.id))) == 3
    assert await session.scalar(select(func.count(Workflow.id))) == 2
