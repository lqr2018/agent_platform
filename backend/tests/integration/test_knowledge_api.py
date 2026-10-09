"""知识库端到端（详细设计 7.6 的测试要求 / 3.2.5 / 4.6.2）。

离线跑通（`VECTOR_STORE_KIND=memory` + `EMBED_ALLOW_FAKE=true` + `kind=fake` 的 Provider）：

1. 上传 md → 轮询到 `ready` → `POST /query` 命中该文档的切片（`FakeEmbedder` 下可确定性断言）；
2. 同 checksum 二次上传**不再调用 embedding**（4.6.2 的幂等）；
3. 删除文档后同一 query 不再命中（7.6 的 DoD："删除文档后再次检索不再命中"）；
4. 未支持的格式在上传时就被 422 拒绝（`KB_UNSUPPORTED_FORMAT`）；
5. `/maintenance/knowledge-bases/{id}/verify-index` 两端计数一致（7.6 的对账）；
6. 绑定 KB 的 Agent 走 Chat 时在**首轮 LLM 之前**做一次固定预检索并注入（4.4.2 / 事件 10），
   未命中、没绑 KB、检索报错都不影响主流程（4.6.3）；
7. Workflow 的 `retriever` 节点复用同一套检索（4.5.2），命中切片进 `output_key`，
   `kb_id` 不存在时按节点默认 `on_error=continue` 记错误码并继续。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ModelProvider, Run, Span
from app.runtime.rag import FakeEmbedder
from app.services import kb_service
from tests.helpers import collect_sse, create_agent, create_conversation, create_fake_provider
from tests.integration.test_workflow_runner import node_runs_of, start_workflow_run, wait_for_terminal

DOC_TEXT = "# 安装\n\n先 clone 仓库，再执行 pip install -e .[dev]，最后跑迁移。\n"
"""测试文档：`MarkdownLoader` 会解析出标题 `安装`（断言引用串用得上）。"""


class _CountingEmbedder:
    """包装 `FakeEmbedder` 并记录每次 `embed()` 的调用（7.6 的"不重复 embed"断言）。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._inner = FakeEmbedder(dim=32)

    @property
    def model(self) -> str:
        return self._inner.model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return await self._inner.embed(texts)


@pytest.fixture(autouse=True)
def _rag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内向量库 + 允许确定性 embedding（SD-11 / 4.1.3）。

    autouse 是必须的：`app` 夹具会 `create_app()`，而配置在那一刻被读取 —— 环境变量必须更早生效。
    """
    monkeypatch.setenv("VECTOR_STORE_KIND", "memory")
    monkeypatch.setenv("EMBED_ALLOW_FAKE", "true")
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def fake_provider(session: AsyncSession) -> ModelProvider:
    """`kind=fake` 的 Provider（4.1.3：只有测试期能直写，API 永远 422）。"""
    provider = ModelProvider(
        name="fake-embedder",
        kind="fake",
        base_url="fake://",
        models=[{"name": "fake-embedding"}],
        default_params={},
        headers={},
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    return provider


@pytest_asyncio.fixture
async def kb(app_client: AsyncClient, fake_provider: ModelProvider) -> str:
    """建一个 KB（`embedding_dim` 由服务层探测固化）并返回 id。"""
    response = await app_client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": "demo-kb",
            "description": "phase 5 的端到端用例",
            "embedding_provider_id": fake_provider.id,
            "embedding_model": "fake-embedding",
            "splitter": "markdown",
            "chunk_size": 200,
            "chunk_overlap": 40,
            "top_k": 3,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


async def _upload(
    client: AsyncClient,
    kb_id: str,
    *,
    filename: str = "install.md",
    content: str = DOC_TEXT,
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code in {202, 422}, response.text
    return dict(response.json())


async def _wait_terminal(client: AsyncClient, kb_id: str, doc_id: str, *, timeout: float = 5.0) -> str:
    """轮询 `documents.status` 到终态（4.6.2 的进度判据就是它）。"""
    deadline = time.monotonic() + timeout
    status = "pending"
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents/{doc_id}")
        status = str(response.json()["data"]["status"])
        if status in {"ready", "failed"}:
            return status
        await asyncio.sleep(0.05)
    return status


async def _ready_document(client: AsyncClient, kb_id: str, *, filename: str = "install.md") -> str:
    payload = await _upload(client, kb_id, filename=filename)
    doc_id = str(payload["data"]["id"])
    assert await _wait_terminal(client, kb_id, doc_id) == "ready"
    return doc_id


async def test_upload_ready_query_and_delete_flow(app_client: AsyncClient, kb: str) -> None:
    """7.6 的主链路 DoD：上传 → 可见切片 → 检索命中（带来源）→ 删除后不再命中。"""
    doc_id = await _ready_document(app_client, kb)

    chunks = await app_client.get(f"/api/v1/knowledge-bases/{kb}/documents/{doc_id}/chunks")
    assert chunks.status_code == 200
    rows = chunks.json()["data"]
    assert len(rows) >= 1
    assert rows[0]["vector_id"] == rows[0]["id"]  # 2.8：切片 id = 向量 id

    query = await app_client.post(f"/api/v1/knowledge-bases/{kb}/query", json={"query": DOC_TEXT, "top_k": 3})
    assert query.status_code == 200, query.text
    data = query.json()["data"]
    assert data["hit_count"] >= 1
    hit = data["chunks"][0]
    assert hit["document_id"] == doc_id
    assert "install.md" in hit["source"]
    assert "安装" in hit["source"]  # `MarkdownLoader` 的标题进入了引用串（4.6.3）

    deleted = await app_client.delete(f"/api/v1/knowledge-bases/{kb}/documents/{doc_id}")
    assert deleted.status_code == 204

    after = await app_client.post(f"/api/v1/knowledge-bases/{kb}/query", json={"query": DOC_TEXT})
    assert after.status_code == 409
    assert after.json()["error"]["code"] == "KB_EMPTY_INDEX"


async def test_second_upload_with_same_checksum_skips_embedding(
    app_client: AsyncClient,
    kb: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.6.2 的幂等：同 checksum 的第二次上传直接复用已有切片，不再付 embedding 费用。"""
    counter = _CountingEmbedder()
    monkeypatch.setattr(kb_service, "build_kb_embedder", lambda *args, **kwargs: counter)

    first_doc = await _ready_document(app_client, kb)
    calls_after_first = len(counter.calls)
    assert calls_after_first >= 1

    second = await _upload(app_client, kb)
    second_doc = str(second["data"]["id"])
    assert second_doc != first_doc
    assert second["data"]["status"] == "ready"  # 复用已有切片 → 直接终态
    assert second["data"]["meta"]["reused_from_document_id"] == first_doc

    await asyncio.sleep(0.1)
    assert len(counter.calls) == calls_after_first, "同 checksum 的二次上传不应再调用 embedding"


async def test_verify_index_matches_between_db_and_collection(app_client: AsyncClient, kb: str) -> None:
    """7.6 的对账：`chunks` 行数 = 向量集合条数。"""
    await _ready_document(app_client, kb)

    response = await app_client.get(f"/api/v1/maintenance/knowledge-bases/{kb}/verify-index")

    assert response.status_code == 200
    payload = response.json()["data"]  # `MaintenanceResultRead`：`ok` / `detail` / `data`（对账明细）
    assert payload["ok"] is True
    counters = payload["data"]
    assert counters["db_chunks"] == counters["vector_items"] >= 1


async def test_unsupported_format_is_rejected_at_upload(app_client: AsyncClient, kb: str) -> None:
    """4.6.1 的收窄口径：pdf 属迭代 E，上传即 422（不进 `failed` 队列）。"""
    payload = await _upload(app_client, kb, filename="manual.pdf", content="%PDF-1.4")

    assert payload["error"]["code"] == "KB_UNSUPPORTED_FORMAT"
    assert payload["error"]["details"]["suffix"] == ".pdf"
    assert "迭代 E" in payload["error"]["details"]["backlog"]


async def test_kb_crud_stats_and_soft_delete(app_client: AsyncClient, kb: str) -> None:
    """3.2.5 的 KB 级 CRUD：列表含 `stats`、`embedding_dim` 已固化、软删后 404。"""
    await _ready_document(app_client, kb)

    listed = await app_client.get("/api/v1/knowledge-bases")
    assert listed.status_code == 200
    item = next(row for row in listed.json()["data"] if row["id"] == kb)
    assert item["stats"]["document_count"] == 1
    assert item["stats"]["chunk_count"] >= 1
    assert item["status"] == "ready"
    assert item["embedding_dim"] == 64  # `FakeEmbedder` 的默认维度（4.1.3 的确定性替身）

    patched = await app_client.patch(f"/api/v1/knowledge-bases/{kb}", json={"description": "改过了"})
    assert patched.status_code == 200
    assert patched.json()["data"]["description"] == "改过了"

    blocked = await app_client.patch(f"/api/v1/knowledge-bases/{kb}", json={"embedding_model": "other-model"})
    assert blocked.status_code == 422  # 2.8：有文档后 embedding_model 禁改

    deleted = await app_client.delete(f"/api/v1/knowledge-bases/{kb}")
    assert deleted.status_code == 204
    missing = await app_client.get(f"/api/v1/knowledge-bases/{kb}")
    assert missing.status_code == 404


async def _post_message(
    client: AsyncClient,
    conversation_id: str,
    content: str,
) -> list[tuple[str, dict[str, object]]]:
    """一轮 Chat（SSE）→ `[(事件, payload)]`（写法与 `test_chat_sse.py` 一致）。"""
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content, "stream": True},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return await collect_sse(response.text)


async def _bind_agent(
    app_client: AsyncClient,
    session: AsyncSession,
    *,
    name: str,
    kb_ids: list[str] | None = None,
) -> str:
    """造 LLM 的 fake Provider → 建 Agent（可绑定 KB）→ 开会话，返回 `conversation_id`。"""
    llm_provider = await create_fake_provider(session, name=f"{name}-provider")
    overrides: dict[str, object] = {"knowledge_base_ids": kb_ids} if kb_ids is not None else {}
    agent = await create_agent(app_client, provider_id=str(llm_provider.id), name=name, **overrides)
    if kb_ids is not None:
        assert agent["knowledge_base_ids"] == kb_ids  # 2.4：Agent 可引用 KB
    return await create_conversation(app_client, agent_id=str(agent["id"]))


async def test_chat_prefetches_bound_knowledge_base(
    app_client: AsyncClient,
    session: AsyncSession,
    kb: str,
) -> None:
    """4.4.2 第 1 步 + 事件 10：绑定 KB 的 Agent 在首轮 LLM 之前做一次固定预检索。

    证据链：`retrieval.completed` 的 `hit_count ≥ 1` → 同名 `retriever` span 的
    `output.chunk_ids` 非空（命中确实交给了 runtime 的 `retrieved_context`）→ 主流程仍然 `succeeded`。
    """
    await _ready_document(app_client, kb)
    conversation_id = await _bind_agent(app_client, session, name="rag-agent", kb_ids=[kb])

    events = await _post_message(app_client, conversation_id, "怎么安装？")
    names = [name for name, _ in events]

    assert names[0] == "run.started" and names[-1] == "done"
    assert "retrieval.completed" in names
    payload = dict(events)["retrieval.completed"]
    assert payload["kb_ids"] == [kb]
    assert payload["query"] == "怎么安装？"
    assert payload["hit_count"] >= 1

    run = (await session.execute(select(Run).where(Run.conversation_id == conversation_id))).scalar_one()
    assert run.status == "succeeded"
    spans = list(
        (await session.execute(select(Span).where(Span.trace_id == run.trace_id).order_by(Span.seq))).scalars().all()
    )
    retriever_spans = [span for span in spans if span.span_type == "retriever"]
    assert len(retriever_spans) == 1  # 每个 Run 只检索一次（4.4.2）
    assert retriever_spans[0].output is not None  # 4.6.3：命中切片进 system 的 `<chunk>` 块
    assert retriever_spans[0].output["chunk_ids"]
    assert retriever_spans[0].attributes["strategy"] == "prefetch"


async def test_chat_retrieval_miss_keeps_run_successful(
    app_client: AsyncClient,
    session: AsyncSession,
    kb: str,
) -> None:
    """4.6.3：未命中不注入空段落，改用"未检索到相关内容"提示；对话照常完成。"""
    conversation_id = await _bind_agent(app_client, session, name="rag-miss-agent", kb_ids=[kb])

    events = await _post_message(app_client, conversation_id, "知识库里没有的东西")
    payload = dict(events)["retrieval.completed"]

    assert payload["hit_count"] == 0
    assert payload["kb_ids"] == [kb]
    assert dict(events)["run.completed"]["status"] == "succeeded"


async def test_chat_without_knowledge_base_emits_no_retrieval_event(
    app_client: AsyncClient,
    session: AsyncSession,
) -> None:
    """未绑定 KB 的 Agent 不发事件 10（避免前端为每次对话都渲染空引用区）。"""
    conversation_id = await _bind_agent(app_client, session, name="plain-agent")

    events = await _post_message(app_client, conversation_id, "你好")
    names = [name for name, _ in events]

    assert "retrieval.completed" not in names
    assert dict(events)["run.completed"]["status"] == "succeeded"


def _retriever_definition(kb_id: str) -> dict[str, Any]:
    """`start → retrieve(retriever) → end` 的最小图（4.5.2；`retriever` 默认 `on_error=continue`）。"""
    return {
        "name": "kb-retriever-flow",
        "nodes": [
            {"id": "start", "type": "start", "next": "retrieve"},
            {
                "id": "retrieve",
                "type": "retriever",
                "name": "检索",
                "kb_id": kb_id,
                "query_template": "{{state.question}}",
                "output_key": "chunks",
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
        "config": {"max_steps": 10, "recursion_limit": 5, "timeout_seconds": 60},
    }


async def _published_workflow(client: AsyncClient, definition: dict[str, Any]) -> str:
    """建图 → 发布（4.5.1：只有 `published` 的图能跑 `POST /runs`），返回 workflow_id。"""
    created = await client.post(
        "/api/v1/workflows",
        json={
            "name": "kb-retriever-flow",
            "description": "retriever 节点接知识库",
            "state_schema": {"question": {"type": "string"}, "chunks": {"type": "array"}},
            "definition": definition,
        },
    )
    assert created.status_code == 201, created.text
    workflow_id = str(created.json()["data"]["id"])
    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert published.status_code == 200, published.text
    return workflow_id


async def _retrieve_node_run(client: AsyncClient, workflow_run_id: str) -> dict[str, Any]:
    rows = await node_runs_of(client, workflow_run_id)
    return next(dict(row) for row in rows if row["node_id"] == "retrieve")


async def test_workflow_retriever_node_reads_knowledge_base(
    app_client: AsyncClient,
    kb: str,
) -> None:
    """4.5.2 / 4.6.3：`retriever` 节点接真实检索，命中切片（带 `source`）进 `output_key`。"""
    doc_id = await _ready_document(app_client, kb)
    workflow_id = await _published_workflow(app_client, _retriever_definition(kb))

    started = await start_workflow_run(app_client, workflow_id, {"question": DOC_TEXT})
    run = await wait_for_terminal(app_client, str(started["id"]))

    assert run["status"] == "succeeded", run
    chunks = run["state"]["chunks"]
    assert chunks, "检索应命中刚上传的文档"
    assert chunks[0]["document_id"] == doc_id
    assert chunks[0]["kb_id"] == kb
    assert "install.md" in chunks[0]["source"] and "安装" in chunks[0]["source"]  # 4.6.3 的引用串
    assert "score=" in chunks[0]["source"]

    retrieve = await _retrieve_node_run(app_client, str(run["id"]))
    assert retrieve["status"] == "succeeded"
    assert retrieve["input"] == {"query": DOC_TEXT, "kb_id": kb}  # `query_template` + `kb_id`（4.5.2）
    assert retrieve["output"]["hit_count"] == len(chunks)
    assert retrieve["output"]["used_kb_ids"] == [kb]
    assert retrieve["output"]["output_key"] == "chunks"


async def test_workflow_retriever_missing_knowledge_base_continues(
    app_client: AsyncClient,
) -> None:
    """`kb_id` 不存在：节点 `failed` + `KB_NOT_FOUND`，Run 仍 `succeeded`（4.5.2 的默认 `continue`）。"""
    workflow_id = await _published_workflow(app_client, _retriever_definition("01MISSINGKB00000000000000"))

    started = await start_workflow_run(app_client, workflow_id, {"question": "任意问题"})
    run = await wait_for_terminal(app_client, str(started["id"]))

    assert run["status"] == "succeeded", run
    retrieve = await _retrieve_node_run(app_client, str(run["id"]))
    assert retrieve["status"] == "failed"
    assert retrieve["error_code"] == "KB_NOT_FOUND"
    assert "[KB_NOT_FOUND]" in str(run["state"]["chunks"])  # 错误文本写进 `output_key`
