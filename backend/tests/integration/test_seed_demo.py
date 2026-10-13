"""`seed_demo` 的集成测试（详细设计 7.9 / 8.1 / 8.5）。

跑的是**脚本自己的函数**（`app.scripts.seed_demo.seed`），环境是临时 SQLite（9.1）+
`kind=fake` 的 Provider（4.1.3）+ 进程内向量库（SD-11），因此覆盖了"配置驱动"最容易出错的几件事：

1. 三份 YAML → 库里的三行 Agent：`tools` / `knowledge_bases` / `workflow` 的 **name 引用被解析成真实 id**；
2. **幂等**（7.9 的 DoD）：连跑两次不新增行、不涨 `workflow.version`、不落多余的 prompt 快照；
3. `--docs` 灌语料：第二次**复用**同 checksum 的文档，切片数与 embedding 都不翻倍；
4. 写错的引用**当面报错**（`ConfigInvalidError`，也就是脚本的退出码 2）：工具名不存在、
   知识库 id 不存在 —— 而不是静默忽略、留下一条"看起来能用"的配置；
5. 配置文件本身出问题（schema / YAML 语法 / 非 UTF-8 编码）与**同名 Agent / Workflow** 也在
   装载前被拒：报错带文件名，且库里不留半份数据（重名会静默覆盖，等于幂等键失效）；
6. Provider 是**每个 Agent 各自**的：YAML 声明的 `model.provider` 各查各的，`provider_ref` 才统一覆盖；
7. `--reset` 的硬删**不留悬空行**（SQLite 没开 FK 级联）：真跑工具链路 + Chat 内联 Workflow 造数据，
   再让一个不在配置里的 Agent 也绑同一张图 —— 它的 Run 属于用户历史，只置空 `workflow_run_id`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConfigInvalidError
from app.db.models import (
    Agent,
    AgentPromptVersion,
    Chunk,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    ModelProvider,
    NodeRun,
    Run,
    Span,
    ToolInvocation,
    Trace,
    Workflow,
    WorkflowRun,
)
from app.runtime.tools.registry import builtin_tool_id
from app.scripts import seed_demo
from app.services import agent_config, task_runner, workflow_config
from tests.helpers import FAKE_MODEL, collect_sse, create_agent, create_conversation, create_fake_provider

CONFIG_DIR = agent_config.CONFIG_DIR
WORKFLOW_DIR = workflow_config.CONFIG_DIR
DOCS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "docs"
AGENT_NAMES = ["Data Analysis Agent", "Knowledge Assistant", "Research Agent"]
SHIPPED_WORKFLOWS = ["created:kb-qa-flow", "created:research-flow"]

AGENT_YAML = """version: 1
agent:
  name: {name}
  model: {{ provider: {provider}, name: {model} }}
{tail}
"""
"""能过 schema 的最小 Agent 配置（`tail` 用来补字段 / 写错引用）。"""

RESEARCH_PROMPT = "调研一下国内主流向量数据库的差异，并给出选型建议"
CALCULATE_PROMPT = "帮我计算 (120+30)*4"
"""与 `tests/e2e/test_demo_agents.py` 同一组问句：`kind=fake` 的 Provider 对它们有确定性响应。"""


def _agent_yaml(name: str = "Broken Agent", *, provider: str = "demo-provider", tail: str = "") -> str:
    """填 `AGENT_YAML`：`provider` 默认就是 `provider` fixture 的名字。"""
    return AGENT_YAML.format(name=name, provider=provider, model=FAKE_MODEL, tail=tail)


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


async def _post_message(client: AsyncClient, conversation_id: str, content: str) -> list[tuple[str, dict[str, Any]]]:
    """发一条消息并解析 SSE（与 `tests/e2e/test_demo_agents.py` 同一写法）。"""
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content, "stream": True},
    )
    assert response.status_code == 200, response.text
    return await collect_sse(response.text)


def _orphan_conditions() -> dict[str, tuple[type[Any], Any]]:
    """悬空引用清单（2.6 / 4.5 的父子关系逐条对齐）：每一条都必须为 0。

    SQLite 没开 `PRAGMA foreign_keys`（`db/session.py` 只设 WAL / `synchronous` / `busy_timeout`），
    模型上 `ondelete=` 写的级联**运行时不生效** —— 删父行时子行只能靠删的人自己收尾，
    这份清单就是那条不变量的可执行定义（`ondelete=SET NULL` 的列要求"保留行、置空指针"）。
    """
    run_ids = select(Run.id)
    workflow_run_ids = select(WorkflowRun.id)
    return {
        "messages → 已删会话": (Message, ~Message.conversation_id.in_(select(Conversation.id))),
        "messages → 已删 Run": (Message, Message.run_id.is_not(None) & ~Message.run_id.in_(run_ids)),
        "traces → 已删 Run": (Trace, ~Trace.run_id.in_(run_ids)),
        "spans → 已删 Trace": (Span, ~Span.trace_id.in_(select(Trace.id))),
        "tool_invocations → 已删 Run": (ToolInvocation, ~ToolInvocation.run_id.in_(run_ids)),
        "agent_prompt_versions → 已删 Agent": (AgentPromptVersion, ~AgentPromptVersion.agent_id.in_(select(Agent.id))),
        "runs → 已删 WorkflowRun": (
            Run,
            Run.workflow_run_id.is_not(None) & ~Run.workflow_run_id.in_(workflow_run_ids),
        ),
        "node_runs → 已删 WorkflowRun": (NodeRun, ~NodeRun.run_id.in_(workflow_run_ids)),
        "workflow_runs → 已删 Workflow": (WorkflowRun, ~WorkflowRun.workflow_id.in_(select(Workflow.id))),
        "agents → 已删 Workflow": (Agent, Agent.workflow_id.is_not(None) & ~Agent.workflow_id.in_(select(Workflow.id))),
    }


async def _count_orphans(session: AsyncSession) -> dict[str, int]:
    """每条悬空引用的行数（全部为 0 才算删干净）。"""
    counts: dict[str, int] = {}
    for label, (model, condition) in _orphan_conditions().items():
        counts[label] = int(await session.scalar(select(func.count()).select_from(model).where(condition)) or 0)
    return counts


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
        _agent_yaml(tail='  tools: ["not_a_tool"]'),
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
        _agent_yaml(tail='  tools: ["calculator"]\n  knowledge_bases: ["01JZZZZZZZZZZZZZZZZZZZZZZZ"]'),
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


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        # schema 不认识的字段（8.1 的 YAML 契约）
        ("unknown-field.yaml", "version: 1\nagent:\n  name: Broken Agent\n  unknown_key: 1\n"),
        # YAML 语法错（PyYAML 自己带行列信息）
        ("syntax-error.yaml", "version: 1\nagent: [oops\n"),
        # 不是 UTF-8（中文 GBK）：`load_agent_config` 一律按 UTF-8 读
        ("gbk-encoded.yaml", "version: 1\nagent:\n  name: 研究 Agent\n".encode()),
    ],
)
@pytest.mark.asyncio
async def test_seed_reports_broken_config_file_with_filename(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
    filename: str,
    payload: str | bytes,
) -> None:
    """配置写错 → `ConfigInvalidError`（脚本退出码 2）：`details` 带**文件名** + 一行可读原因。"""
    config_dir = tmp_path / "agents"
    config_dir.mkdir()
    path = config_dir / filename
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConfigInvalidError) as excinfo:
        await _seed(session, provider, config_dir=config_dir)

    assert excinfo.value.details["file"] == str(path)
    assert "Invalid agent config file" in excinfo.value.message
    error = str(excinfo.value.details["error"])
    assert error and "\n" not in error  # 压成一行：`demo.ps1` 的失败提示要能直接读
    # 配置没过就在写库前失败：不会留下"半个 Agent"这种更难查的状态
    assert await session.scalar(select(func.count(Agent.id))) == 0


@pytest.mark.asyncio
async def test_seed_reports_broken_workflow_config_file(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
) -> None:
    """Workflow 那条装载路径同样收口（`--workflow-dir` 指错时不会漏出 PyYAML 的裸异常）。"""
    config_dir = tmp_path / "agents"
    workflow_dir = tmp_path / "workflows"
    _write_agent(config_dir, "a.yaml", _agent_yaml())
    broken = _write_agent(workflow_dir, "bad-flow.yaml", "version: 1\nworkflow: [oops\n")

    with pytest.raises(ConfigInvalidError) as excinfo:
        await _seed(session, provider, config_dir=config_dir, workflow_dir=workflow_dir)

    assert excinfo.value.details["file"] == str(broken)
    assert "Invalid workflow config file" in excinfo.value.message


@pytest.mark.parametrize("kind", ["agent", "workflow"])
@pytest.mark.asyncio
async def test_seed_rejects_duplicate_names_before_writing(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
    kind: str,
) -> None:
    """同名配置 = 幂等键冲突（后一份会静默覆盖前一份）：装载前拒绝，且库里不留半份数据。"""
    config_dir = tmp_path / "agents"
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    _write_agent(config_dir, "a.yaml", _agent_yaml())
    if kind == "agent":
        duplicate_name = "Twin Agent"
        duplicated = _agent_yaml(name=duplicate_name)
        twin_dir = config_dir
    else:
        source = sorted(WORKFLOW_DIR.glob("*.yaml"))[0]
        duplicate_name = workflow_config.load_workflow_config(source).workflow.name
        duplicated = source.read_text(encoding="utf-8")
        twin_dir = workflow_dir
    _write_agent(twin_dir, "twin-a.yaml", duplicated)
    _write_agent(twin_dir, "twin-b.yaml", duplicated)

    with pytest.raises(ConfigInvalidError) as excinfo:
        await _seed(session, provider, config_dir=config_dir, workflow_dir=workflow_dir)

    assert excinfo.value.details[f"{kind}_names"] == [duplicate_name]
    assert await session.scalar(select(func.count(Agent.id))) == 0
    assert await session.scalar(select(func.count(Workflow.id))) == 0


@pytest.mark.asyncio
async def test_seed_resolves_provider_per_agent(
    session: AsyncSession,
    provider: ModelProvider,
    tmp_path: Path,
) -> None:
    """Provider 是**每个 Agent 各自**的：YAML 里的 `model.provider` 各查各的；传 `provider_ref` 才统一覆盖。"""
    other = await create_fake_provider(session, name="second-provider")
    config_dir = tmp_path / "agents"
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    _write_agent(config_dir, "a.yaml", _agent_yaml(name="Probe Agent A", provider=provider.name))
    _write_agent(config_dir, "b.yaml", _agent_yaml(name="Probe Agent B", provider=other.name))

    # `provider_ref=None` = CLI 不带 `--provider`：这时才走"每个 Agent 用自己 YAML 的 provider"
    await _seed(session, provider, config_dir=config_dir, workflow_dir=workflow_dir, provider_ref=None)

    agents = await _agents(session)
    assert agents["Probe Agent A"].model_provider_id == provider.id
    assert agents["Probe Agent B"].model_provider_id == other.id

    # `--provider second-provider`（= 传了 `provider_ref`）：两个 Agent 都被覆盖成同一个
    await _seed(session, provider, config_dir=config_dir, workflow_dir=workflow_dir, provider_ref=other.name)

    agents = await _agents(session)
    assert {agents["Probe Agent A"].model_provider_id, agents["Probe Agent B"].model_provider_id} == {other.id}


@pytest.mark.asyncio
async def test_seed_reset_leaves_no_dangling_references(
    app_client: AsyncClient,
    session: AsyncSession,
    provider: ModelProvider,
) -> None:
    """`--reset` 的硬删必须**不留悬空行**，也不许动别人的资产。

    真跑两条链路造数据：工具链路（messages / runs / traces / spans / tool_invocations）与 Chat 内联
    Workflow（workflow_runs / node_runs + `runs.workflow_run_id`）；再让一个**不在配置里**的 Agent 也绑
    同一张图（`agents.workflow_id`）并跑一轮 —— 它属于用户资产，`--reset` 只能置空指针，不能删行。
    """
    await _seed(session, provider)
    agents = await _agents(session)

    # 1) 工具链路（数据型）：一轮消息一条 Run + Trace + Span + 一次工具调用
    data_conversation = await create_conversation(app_client, agent_id=str(agents["Data Analysis Agent"].id))
    await _post_message(app_client, data_conversation, CALCULATE_PROMPT)

    # 2) Chat 内联 Workflow（研究型）：图跑完才有 workflow_runs / node_runs
    research_conversation = await create_conversation(app_client, agent_id=str(agents["Research Agent"].id))
    await _post_message(app_client, research_conversation, RESEARCH_PROMPT)

    # 3) 旁观者：用户自己的 Agent 也绑了 research-flow（`--reset` 只删配置里声明的那几个 name）
    workflow = await _one_workflow(session, "research-flow")
    bystander = await create_agent(
        app_client, provider_id=provider.id, name="My Research Agent", workflow_id=workflow.id
    )
    bystander_conversation = await create_conversation(app_client, agent_id=str(bystander["id"]))
    await _post_message(app_client, bystander_conversation, RESEARCH_PROMPT)
    bystander_run = (
        await session.execute(select(Run).where(Run.conversation_id == bystander_conversation))
    ).scalar_one()
    assert bystander_run.workflow_run_id is not None

    # 前置自检：这些数据本来就是干净的（否则下面的断言等于没证）
    assert set((await _count_orphans(session)).values()) == {0}
    bystander_messages = await session.scalar(
        select(func.count(Message.id)).where(Message.conversation_id == bystander_conversation)
    )
    assert bystander_messages is not None and bystander_messages >= 2  # 至少一问一答

    await _seed(session, provider, reset=True)

    assert await _count_orphans(session) == dict.fromkeys(_orphan_conditions(), 0)
    # "历史不能丢"（1.5.5）：旁观者的 Run / 消息一条不少，只是指针被显式置空
    await session.refresh(bystander_run)
    assert (bystander_run.status, bystander_run.workflow_run_id) == ("succeeded", None)
    remaining = await session.scalar(
        select(func.count(Message.id)).where(Message.conversation_id == bystander_conversation)
    )
    assert remaining == bystander_messages
    bystander_row = await session.get(Agent, str(bystander["id"]))
    assert bystander_row is not None and bystander_row.workflow_id is None
