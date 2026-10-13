"""一键装载第 8 章的三个 Demo Agent（详细设计 7.9 交付物 / 8.1 / 8.5）。

用法（在 `backend/` 下；容器里 `docker compose exec api python -m app.scripts.seed_demo`）::

    python -m app.scripts.seed_demo                       # 幂等装载：KB / Workflow / Agent
    python -m app.scripts.seed_demo --docs                # 再把 tests/fixtures/docs 灌进 KB
    python -m app.scripts.seed_demo --reset               # 先删同名 Agent（重放演示从干净状态开始）
    python -m app.scripts.seed_demo --provider my-provider --model qwen-plus

装载顺序（引用必须先生成）：

1. 内置工具对齐（`tool_service.sync_builtin_tools`：代码是唯一事实来源）；
2. Provider 解析：**每个 Agent 用自己 YAML 里的 `model.provider`**（按 name 解析）；`--provider` 传了就
   统一覆盖全部 Agent，YAML 里的名字库里没有 → 退回平台默认 Provider；
3. 知识库（YAML 引用到但库里没有的**按 name 创建**，用 embedding provider；已存在则原样复用）；
4. Agent 第一遍（`upsert_agent`，不带 `workflow_id`）—— 先把 name → id 定下来；
5. Workflow（`configs/workflows/*.yaml`，`agent_id` / `kb_id` 占位名解析成真实 id，然后发布）；
6. Agent 第二遍（回填 `workflow_id`；YAML 里 `workflow: null` 的 Agent 跳过）；
7. 可选：`--docs` 上传演示语料并**等它 `ready`**（摄取仍走进程内 `TaskRunner`，脚本退出前收尾）。

零代码差异（8.5）：本脚本只把 YAML 翻译成 API 的入参，**不含任何 Agent 专属分支** ——
新增一个 Demo 只需新增一份 YAML（`tests/unit/test_demo_genericity.py` 扫描 `app/**` 复核）。

`--reset` 是**硬删**：同名 Agent / Workflow 连同它的会话、消息、Run、Trace、Span、工具调用记录一起
删掉（SQLite 没开 FK 级联，脚本里显式收尾 —— 不留悬空指针给 `/traces`、`/runs` 去踩）。
**别人的东西不动**：不挂在配置里的 Agent，它跑过的 Run（哪怕跑的就是这张图）与它自己绑的这张图
都保留下来，只把 `runs.workflow_run_id` / `agents.workflow_id` 显式置空（模型声明的是 `SET NULL`）。

退出码：`0` = 全部就绪；`2` = 环境/配置问题（库未迁移、配置写错或重名、provider / tool 名字写错、
embedding 不可用、演示语料目录不存在或摄取失败）。配置文件的解析 / 编码错误也走 `2`（带文件名），
不再是一串裸 traceback。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.enums import DocumentStatus
from app.core.errors import AppError, ConfigInvalidError
from app.db.models import (
    Agent,
    AgentPromptVersion,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    ModelProvider,
    NodeRun,
    Run,
    Span,
    Tool,
    ToolInvocation,
    Trace,
    Workflow,
    WorkflowRun,
)
from app.db.session import check_database, dispose_engine, get_sessionmaker
from app.schemas.agent import AgentCreate, MemoryConfigDTO
from app.schemas.knowledge import KnowledgeBaseCreate
from app.schemas.workflow import WorkflowCreate
from app.services import (
    agent_config,
    agent_service,
    kb_service,
    model_provider_service,
    task_runner,
    tool_service,
    workflow_config,
    workflow_service,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
"""环境/配置问题（与脚本自身的 bug 区分开，和 `check_readyz.py` / `evaluate.py` 同一约定）。"""

DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "docs"
"""`--docs` 不带值时用的演示语料目录（Phase 5 的测试语料复用给 Demo 3）。"""

DOC_MIME_TYPES = {".md": "text/markdown", ".txt": "text/plain"}
DOC_SUFFIXES = tuple(DOC_MIME_TYPES)

DOC_WAIT_SECONDS = 60.0
"""单个文档等摄取跑到终态的上限（秒）。"""

DOC_POLL_SECONDS = 0.05
"""轮询间隔：worker 与脚本在同一事件循环里，`await asyncio.sleep` 就是把控制权让给它。"""

DOC_TERMINAL_STATUSES = (str(DocumentStatus.READY), str(DocumentStatus.FAILED))
"""`documents.status` 的终态（4.6.2 的状态机；`missing` 由 `_wait_document` 的 `None` 分支覆盖）。"""


@dataclass
class SeedReport:
    """装载结果（打印给用户看，测试也直接断言它）。"""

    knowledge_bases: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "Demo seed complete:",
            f"  knowledge bases: {', '.join(self.knowledge_bases) or '-'}",
            f"  workflows:       {', '.join(self.workflows) or '-'}",
            f"  agents:          {', '.join(self.agents) or '-'}",
        ]
        if self.documents:
            lines.append(f"  documents:       {', '.join(self.documents)}")
        return "\n".join(lines)


async def seed(
    session: AsyncSession,
    settings: Settings,
    *,
    config_dir: Path,
    workflow_dir: Path,
    provider_ref: str | None = None,
    model_name: str | None = None,
    embedding_provider_ref: str | None = None,
    embedding_model: str | None = None,
    docs_dir: Path | None = None,
    reset: bool = False,
) -> SeedReport:
    """把 `configs/agents/*.yaml` + `configs/workflows/*.yaml` 装载进库（可反复执行）。"""
    report = SeedReport()
    await tool_service.sync_builtin_tools(session)
    tools = {row.name: row for row in await tool_service.list_tools(session)}

    agent_configs = _load_config_files(
        agent_config.list_agent_config_paths(config_dir), agent_config.load_agent_config, kind="agent"
    )
    workflow_configs = _load_config_files(
        workflow_config.list_workflow_config_paths(workflow_dir), workflow_config.load_workflow_config, kind="workflow"
    )
    if not agent_configs:
        raise ConfigInvalidError("No agent configs found", details={"directory": str(config_dir)})
    # name 就是幂等键（8.1）：同名配置会让后一份静默覆盖前一份，装载期直接拒绝
    _reject_duplicate_names([cfg.agent.name for cfg in agent_configs], kind="agent")
    _reject_duplicate_names([cfg.workflow.name for cfg in workflow_configs], kind="workflow")

    if reset:
        for agent_cfg in agent_configs:
            await _reset_agent_by_name(session, agent_cfg.agent.name)
        for workflow_cfg in workflow_configs:
            await _reset_workflow_by_name(session, workflow_cfg.workflow.name)

    # 2) Provider 解析：按每个 Agent 自己的 `model.provider` 引用去查（`--provider` 统一覆盖）
    provider_refs = sorted({provider_ref or cfg.agent.model.provider for cfg in agent_configs})
    providers = {ref: await _resolve_provider(session, ref, role="model") for ref in provider_refs}

    def provider_id_for(body: agent_config.AgentConfigBody) -> str:
        """该 Agent 绑定的 Provider id（YAML 的 `model.provider`；`--provider` 传了就用它）。"""
        return providers[provider_ref or body.model.provider].id

    # 3) 知识库：把 YAML 引用到的 name 先建出来（Agent 与 Workflow 都要用它的 id）
    kb_refs = _collect_kb_refs(agent_configs, workflow_configs)
    kb_ids: dict[str, str] = {}
    for kb_ref in kb_refs:
        kb, created = await _ensure_knowledge_base(
            session,
            kb_ref,
            settings=settings,
            provider_ref=embedding_provider_ref,
            model_name=embedding_model,
        )
        kb_ids[kb_ref] = kb.id
        kb_ids.setdefault(kb.name, kb.id)
        if created:
            report.knowledge_bases.append(f"created:{kb.name}")

    # 4) Agent 第一遍：先定 name → id（Workflow 的 `agent_id` 占位要用）
    agent_ids: dict[str, str] = {}
    for agent_cfg in agent_configs:
        body = agent_cfg.agent
        agent, created = await agent_service.upsert_agent(
            session,
            _to_agent_create(
                body,
                provider_id=provider_id_for(body),
                model_name=model_name or body.model.name,
                tool_ids=_tool_ids(body, tools),
                knowledge_base_ids=_knowledge_base_ids(body, kb_ids),
                workflow_id=None,
            ),
        )
        agent_ids[body.name] = agent.id
        report.agents.append(f"{'created' if created else 'updated'}:{body.name}")

    # 5) Workflow：占位名解析 → 写库 → 发布
    workflow_ids: dict[str, str] = {}
    for workflow_cfg in workflow_configs:
        definition, unresolved = _resolve_definition(
            workflow_cfg.workflow.definition, agent_ids=agent_ids, kb_ids=kb_ids
        )
        if unresolved:
            raise ConfigInvalidError(
                "Workflow config references unknown name(s)",
                details={"workflow": workflow_cfg.workflow.name, "unresolved": unresolved},
            )
        row, action = await workflow_service.upsert_workflow(
            session,
            WorkflowCreate(
                name=workflow_cfg.workflow.name,
                description=workflow_cfg.workflow.description,
                definition=definition,
                state_schema=dict(workflow_cfg.workflow.state_schema),
                status="published",
            ),
        )
        workflow_ids[workflow_cfg.workflow.name] = row.id
        report.workflows.append(f"{action}:{row.name}")

    # 6) Agent 第二遍：回填 `workflow_id`（YAML 里没写 workflow 的 Agent 不重复写库）
    for agent_cfg in agent_configs:
        body = agent_cfg.agent
        if not body.workflow:
            continue
        if body.workflow not in workflow_ids:
            raise ConfigInvalidError(
                "Agent config references unknown workflow",
                details={"agent": body.name, "workflow": body.workflow},
            )
        await agent_service.upsert_agent(
            session,
            _to_agent_create(
                body,
                provider_id=provider_id_for(body),
                model_name=model_name or body.model.name,
                tool_ids=_tool_ids(body, tools),
                knowledge_base_ids=_knowledge_base_ids(body, kb_ids),
                workflow_id=workflow_ids[body.workflow],
            ),
        )

    # 7) 可选：演示语料（同步摄取，脚本退出前必须 ready）
    if docs_dir is not None:
        for kb_ref in kb_refs:
            kb_row = await session.get(KnowledgeBase, kb_ids[kb_ref])
            if kb_row is not None:
                report.documents.extend(await _ingest_docs(session, kb=kb_row, directory=docs_dir))
    return report


_ConfigT = TypeVar("_ConfigT")

CONFIG_FILE_ERRORS = (ValidationError, yaml.YAMLError, UnicodeDecodeError, OSError)
"""配置文件层面的失败（schema 不合法 / YAML 语法错 / 编码错 / 读不了）。

`agent_config` / `workflow_config` 刻意保留 pydantic / PyYAML 的原始异常（结构化、可编程处理，
见两个模块的 docstring）；脚本这一层要的是"写错文件 → 退出码 2 + 一行可读信息"，因此在这里收口 ——
与 `evaluate.py` 的 `YamlSuiteSource.load()`（同样把配置问题变成 `CONFIG_INVALID`）同一约定。
"""


def _load_config_files(paths: Sequence[Path], loader: Callable[[Path], _ConfigT], *, kind: str) -> list[_ConfigT]:
    """逐个文件装载；任何一个文件不合法 → `ConfigInvalidError`（details 带文件名与首条错误）。"""
    loaded: list[_ConfigT] = []
    for path in paths:
        try:
            loaded.append(loader(path))
        except CONFIG_FILE_ERRORS as exc:
            raise ConfigInvalidError(
                f"Invalid {kind} config file: {path.name}",
                details={"file": str(path), "error": _describe_config_error(exc)},
            ) from exc
    return loaded


def _describe_config_error(exc: Exception) -> str:
    """把多行报错压成一行（`stderr` 一行一条，方便 `demo.ps1` 的失败提示）。"""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", ""))
        return f"{where}: {message}" if where else message
    if isinstance(exc, yaml.YAMLError):
        return " ".join(str(exc).split())  # PyYAML 的报错自带行列信息，压成一行即可
    return f"{type(exc).__name__}: {exc}"


def _reject_duplicate_names(names: Sequence[str], *, kind: str) -> None:
    """同名配置 = 幂等键冲突（8.1 的 `name`）：后一份会**静默覆盖**前一份，宁可失败。"""
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigInvalidError(f"Duplicate {kind} name(s) in configs", details={f"{kind}_names": duplicates})


def _tool_ids(body: agent_config.AgentConfigBody, tools: Mapping[str, Tool]) -> list[str]:
    """YAML 的 `tools[]` → `tools.id`；**名字写错直接报错**（不静默忽略：宁可启动失败，不要"演示点不亮"）。"""
    unknown = [name for name in body.tools if name not in tools]
    if unknown:
        raise ConfigInvalidError(
            "Agent config references unknown tool(s)",
            details={"agent": body.name, "unknown": unknown},
        )
    return [tools[name].id for name in body.tools]


def _knowledge_base_ids(body: agent_config.AgentConfigBody, kb_ids: Mapping[str, str]) -> list[str]:
    """YAML 的 `knowledge_bases[]` → KB id（`kb_ids` 里同时有 name 与 id 两种键）。"""
    unknown = [ref for ref in body.knowledge_bases if ref not in kb_ids]
    if unknown:
        raise ConfigInvalidError(
            "Agent config references unknown knowledge base(s)",
            details={"agent": body.name, "unknown": unknown},
        )
    return [kb_ids[ref] for ref in body.knowledge_bases]


def _to_agent_create(
    body: agent_config.AgentConfigBody,
    *,
    provider_id: str,
    model_name: str,
    tool_ids: Sequence[str],
    knowledge_base_ids: Sequence[str],
    workflow_id: str | None,
) -> AgentCreate:
    """YAML → `AgentCreate`（**唯一的一处翻译**，不含任何 Agent 专属分支，8.5）。"""
    return AgentCreate(
        name=body.name,
        description=body.description,
        model_provider_id=provider_id,
        model_name=model_name,
        model_params=dict(body.model.params),
        system_prompt=body.system_prompt,
        tool_ids=list(tool_ids),
        knowledge_base_ids=list(knowledge_base_ids),
        memory_config=MemoryConfigDTO.model_validate(body.memory.model_dump()),
        workflow_id=workflow_id,
        max_steps=body.max_steps,
        timeout_seconds=body.timeout_seconds,
        tags=list(body.tags),
        is_template=body.is_template,
        status=body.status,
    )


def _collect_kb_refs(
    agent_configs: Sequence[agent_config.AgentConfigFile],
    workflow_configs: Sequence[workflow_config.WorkflowConfigFile],
) -> list[str]:
    """收集 YAML 引用到的知识库 name / id（Agent 的 `knowledge_bases` + 图的 `kb_id`），排序去重。

    两种写法都收（`--reset` 之外的幂等装载要求"配置里写的每一个引用都被解析"）：
    `_ensure_knowledge_base` 会先按 name 再按 id 查库，因此这里不需要（也不该）提前猜类型。
    """
    refs: set[str] = set()
    for agent_cfg in agent_configs:
        refs.update(ref for ref in agent_cfg.agent.knowledge_bases if ref)
    for workflow_cfg in workflow_configs:
        for node in workflow_cfg.workflow.definition.get("nodes", []):
            ref = node.get("kb_id") if isinstance(node, Mapping) else None
            if isinstance(ref, str) and ref:
                refs.add(ref)
    return sorted(refs)


def _resolve_definition(
    definition: Mapping[str, Any],
    *,
    agent_ids: Mapping[str, str],
    kb_ids: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """把图里的 name 占位替换成真实 id（深拷贝，不改配置对象）；返回 `(definition, 未解析项)`。"""
    resolved = copy.deepcopy(dict(definition))
    unresolved: list[str] = []
    nodes = resolved.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key, table in (("agent_id", agent_ids), ("kb_id", kb_ids)):
                value = node.get(key)
                if not isinstance(value, str) or not value or _looks_like_id(value):
                    continue
                if value in table:
                    node[key] = table[value]
                else:
                    unresolved.append(f"node={node.get('id')} {key}={value}")
    return resolved, unresolved


def _looks_like_id(value: str) -> bool:
    """ULID 固定 26 字符（2.2）：图中直接写真实 id 时跳过 name 解析。"""
    return len(value) == 26 and value.isalnum()


async def _resolve_provider(session: AsyncSession, ref: str | None, *, role: str) -> ModelProvider:
    """按 name / id 找 Provider；找不到就退回平台默认（连默认都没有 → 配置错误，退出码 2）。"""
    if ref:
        row = (
            (
                await session.execute(
                    select(ModelProvider).where(or_(ModelProvider.name == ref, ModelProvider.id == ref))
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return row
    fallback = await model_provider_service.ensure_default_provider(session)
    if fallback is None:
        raise ConfigInvalidError(
            f"No {role} provider available",
            details={"provider": ref or "", "hint": "先在 /model-providers 建一个，或传 --provider"},
        )
    if ref:
        print(  # noqa: T201 - 脚本的进度输出
            f"[seed] {role} provider '{ref}' not found; falling back to '{fallback.name}'",
            file=sys.stderr,
        )
    else:
        print(  # noqa: T201 - 常态：没传 --provider / --embedding-provider，本来就用平台默认
            f"[seed] {role} provider: none specified, using platform default '{fallback.name}'",
            file=sys.stderr,
        )
    return fallback


async def _ensure_knowledge_base(
    session: AsyncSession,
    ref: str,
    *,
    settings: Settings,
    provider_ref: str | None,
    model_name: str | None,
) -> tuple[KnowledgeBase, bool]:
    """按 name / id 找 KB；没有就**按 name 建一个**（embedding provider 解析后探测 `embedding_dim`）。"""
    existing = (
        (
            await session.execute(
                select(KnowledgeBase).where(
                    or_(KnowledgeBase.name == ref, KnowledgeBase.id == ref),
                    KnowledgeBase.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing, False
    if _looks_like_id(ref):
        raise ConfigInvalidError(
            "Knowledge base id not found",
            details={"kb": ref, "hint": "配置里按 name 引用（写 id 就必须是库里已存在的 KB）"},
        )

    provider = await _resolve_provider(session, provider_ref, role="embedding")
    resolved_model = model_name or provider.default_model
    if not resolved_model:
        raise ConfigInvalidError(
            "embedding model is required",
            details={"kb": ref, "provider": provider.name, "hint": "传 --embedding-model"},
        )
    kb = await kb_service.create_knowledge_base(
        session,
        KnowledgeBaseCreate(
            name=ref,
            description=f"Demo knowledge base (seeded): {ref}",
            embedding_provider_id=provider.id,
            embedding_model=resolved_model,
            splitter="markdown",
            chunk_size=800,
            chunk_overlap=120,
            top_k=5,
            score_threshold=0.3,
        ),
        settings=settings,
    )
    return kb, True


async def _ingest_docs(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    directory: Path,
) -> list[str]:
    """把目录里的 `.md` / `.txt` 灌进 KB 并等摄取跑到终态（4.6.2 的状态机）。

    摄取走的是**正式路径**：`create_document` 落库后交给进程内 `TaskRunner`（任务会在同一事件
    循环里被 worker 取走），脚本只负责"等它 `ready`"。**不**在这里再调一次
    `kb_service.ingest_document` —— 那会让同一份文档被摄取两次（重复切片、重复 embedding）。

    同 checksum 已在库的文档会被 `create_document` 判为复用（直接 `ready`），因此
    **重复执行不会重复 embedding**（7.9 的幂等 DoD）。
    """
    if not directory.is_dir():
        raise ConfigInvalidError("Docs directory not found", details={"directory": str(directory)})
    # 快照成字符串：`create_document` 内部会多次 commit / 探针会 rollback，ORM 对象可能过期，
    # 过期属性在同步上下文里被访问会触发 `MissingGreenlet`
    kb_id, kb_name = kb.id, kb.name
    results: list[str] = []
    for path in sorted(item for item in directory.iterdir() if item.suffix in DOC_SUFFIXES):
        document = await kb_service.create_document(
            session,
            kb_id,
            filename=path.name,
            content=path.read_bytes(),
            mime_type=DOC_MIME_TYPES[path.suffix],
        )
        document_id = document.id
        status = str(document.status)
        if status == str(DocumentStatus.READY):
            results.append(f"reused:{path.name}")
            continue
        status = await _wait_document(document_id)
        results.append(f"{status}:{path.name}")
        if status != str(DocumentStatus.READY):
            raise ConfigInvalidError(
                "Document ingestion failed",
                details={"kb": kb_name, "filename": path.name, "status": status},
            )
    return results


async def _wait_document(document_id: str) -> str:
    """用**独立的探针 session** 轮询 `documents.status` 到终态并返回它。

    独立 session 是刻意的：摄取由 `TaskRunner` 的 worker 写（4.6.2），探针只读 —— 既不和
    worker 抢写锁，也不会 `rollback()` 掉调用方 session 里的 ORM 对象（rollback 会让对象过期，
    之后在同步上下文里访问属性会抛 `MissingGreenlet`）。每轮先 `rollback()` 结束读事务，
    这样拿到的是 worker 的最新提交。
    """
    deadline = asyncio.get_running_loop().time() + DOC_WAIT_SECONDS
    async with get_sessionmaker()() as probe:
        while True:
            await probe.rollback()
            status = await probe.scalar(select(Document.status).where(Document.id == document_id))
            if status is None:
                raise ConfigInvalidError("Document disappeared during ingestion", details={"document_id": document_id})
            if str(status) in DOC_TERMINAL_STATUSES:
                return str(status)
            if asyncio.get_running_loop().time() > deadline:
                raise ConfigInvalidError(
                    "Document ingestion timed out",
                    details={"document_id": document_id, "status": str(status), "timeout_seconds": DOC_WAIT_SECONDS},
                )
            await asyncio.sleep(DOC_POLL_SECONDS)


async def _reset_agent_by_name(session: AsyncSession, name: str) -> None:
    """`--reset`：硬删同名 Agent 及其会话 / 消息 / Run / Trace / Span / 工具调用 / prompt 快照。

    SQLite **没开** `PRAGMA foreign_keys`（`db/session.py` 只设 WAL / `synchronous` / `busy_timeout`），
    模型上 `ForeignKey(..., ondelete=...)` 的级联**运行时并不生效** —— 所以这里逐表显式删，否则
    `--reset` 会留下悬空行（典型症状：`/traces` 里有一条 Run 已经不存在的 Trace）。
    删除范围与模型声明的语义逐条对齐：`runs.agent_id` 是 `SET NULL`，其子行一律跟着走。
    """
    rows = (await session.execute(select(Agent).where(Agent.name == name))).scalars().all()
    for row in rows:
        run_ids = select(Run.id).where(Run.agent_id == row.id)
        conversation_ids = select(Conversation.id).where(Conversation.agent_id == row.id)
        # spans / traces / tool_invocations 的 run_id 是 CASCADE：Run 没了它们也不该留
        await session.execute(delete(Span).where(Span.trace_id.in_(select(Trace.id).where(Trace.run_id.in_(run_ids)))))
        await session.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
        await session.execute(delete(ToolInvocation).where(ToolInvocation.run_id.in_(run_ids)))
        # 会话没了 → 它的消息跟着走（messages.conversation_id 是 CASCADE）
        await session.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        # 会话还在、只是 Run 没了的消息**保留**（messages.run_id 是 SET NULL，历史不能丢内容）
        await session.execute(update(Message).where(Message.run_id.in_(run_ids)).values(run_id=None))
        await session.execute(delete(Run).where(Run.agent_id == row.id))
        await session.execute(delete(Conversation).where(Conversation.agent_id == row.id))
        await session.execute(delete(AgentPromptVersion).where(AgentPromptVersion.agent_id == row.id))
        await session.delete(row)
    if rows:
        await session.commit()


async def _reset_workflow_by_name(session: AsyncSession, name: str) -> None:
    """`--reset`：硬删同名 Workflow 及其运行 / 节点记录。

    `runs.workflow_run_id` / `agents.workflow_id` 都是 `SET NULL`：别的 Agent 也跑过 / 绑过这张图时，
    那条 Run 与那个 Agent 属于**用户资产** —— 不能跟着删，只能把指针显式置空（这就是"不留悬空指针"
    的完整含义：删完之后 `/agents`、`/chat` 不该拿一个已删的 workflow id 去查）。
    """
    rows = (await session.execute(select(Workflow).where(Workflow.name == name))).scalars().all()
    for row in rows:
        run_ids = select(WorkflowRun.id).where(WorkflowRun.workflow_id == row.id)
        await session.execute(update(Run).where(Run.workflow_run_id.in_(run_ids)).values(workflow_run_id=None))
        await session.execute(update(Agent).where(Agent.workflow_id == row.id).values(workflow_id=None))
        await session.execute(delete(NodeRun).where(NodeRun.run_id.in_(run_ids)))
        await session.execute(delete(WorkflowRun).where(WorkflowRun.workflow_id == row.id))
        await session.delete(row)
    if rows:
        await session.commit()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.seed_demo",
        description="装载 configs/agents/*.yaml 与 configs/workflows/*.yaml（幂等，可反复执行）",
    )
    parser.add_argument("--config-dir", default=str(agent_config.CONFIG_DIR), help="Agent 配置目录")
    parser.add_argument("--workflow-dir", default=str(workflow_config.CONFIG_DIR), help="Workflow 配置目录")
    parser.add_argument("--provider", default=None, help="覆盖 YAML 里的 model.provider（name 或 id）")
    parser.add_argument("--model", default=None, help="覆盖 YAML 里的 model.name")
    parser.add_argument("--embedding-provider", default=None, help="知识库用的 Provider（默认平台默认 Provider）")
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="知识库使用的 embedding 模型（默认取 Provider 的 default_model）",
    )
    parser.add_argument(
        "--docs",
        nargs="?",
        const=str(DEFAULT_DOCS_DIR),
        default=None,
        help="顺带把目录里的 .md/.txt 灌进知识库并同步摄取（默认目录：tests/fixtures/docs）",
    )
    parser.add_argument("--reset", action="store_true", help="先删同名 Agent / Workflow，再从零装载")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    ready, detail = await check_database()
    if not ready:
        print(f"[seed] database not ready: {detail}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG_ERROR
    docs_dir = Path(args.docs) if args.docs else None
    try:
        async with get_sessionmaker()() as session:
            report = await seed(
                session,
                settings,
                config_dir=Path(args.config_dir),
                workflow_dir=Path(args.workflow_dir),
                provider_ref=args.provider,
                model_name=args.model,
                embedding_provider_ref=args.embedding_provider,
                embedding_model=args.embedding_model,
                docs_dir=docs_dir,
                reset=bool(args.reset),
            )
    except AppError as exc:
        print(f"[seed] {exc.code}: {exc.message} {exc.details}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG_ERROR
    finally:
        # 脚本是短命进程：把队列里可能还挂着的摄取收尾（4.6.2 / task_runner 的第 3 条）
        await task_runner.get_task_runner(settings).stop()
    print(report.render())  # noqa: T201
    print("Next: 打开 /agents 或在 /chat 里选一个 Demo Agent 提问（README 的“三大 Demo 演示脚本”）")  # noqa: T201
    return EXIT_OK


async def _main(argv: Sequence[str] | None = None) -> int:
    try:
        return await _run(_parse_args(argv))
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """控制台入口（`main()` 只负责把异步主流程跑完）。"""
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
