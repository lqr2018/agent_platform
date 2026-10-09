"""检索器单测（详细设计 4.6.1 / 4.6.3 / 7.6 任务 6）。

用 `FakeEmbedder` + `InMemoryVectorStore` 离线验证：命中排序、阈值过滤、多 KB 合并、
`top_k` 截断、空 query / 空 KB 的短路，以及 4.4.2 / 4.6.3 两种注入格式的统一。
"""

from __future__ import annotations

from app.runtime.rag.base import RetrievalConfig, RetrievedChunk, VectorItem
from app.runtime.rag.embedders import FakeEmbedder
from app.runtime.rag.retriever import (
    EMPTY_RETRIEVAL_NOTICE,
    Retriever,
    build_context_blocks,
    format_citation_list,
)
from app.runtime.rag.vectorstores import InMemoryVectorStore

INSTALL_TEXT = "安装步骤：先 clone 仓库，再执行 pip install -e .[dev]，最后跑迁移。"
CONFIG_TEXT = "配置说明：把 MODEL_PROVIDER 写进 .env，SECRET_KEY 在生产必须覆盖。"
UNRELATED_TEXT = "python 单元测试用 pytest 与 httpx MockTransport，不引入 respx。"


async def _retriever_with_kb(*, collection: str, docs: list[tuple[str, str]], dim: int = 64) -> Retriever:
    """建一个填好向量的 store；`docs` 是 `(chunk_id, content)`。"""
    embedder = FakeEmbedder(dim=dim)
    store = InMemoryVectorStore()
    vectors = await embedder.embed([content for _, content in docs])
    await store.upsert(
        collection,
        [
            VectorItem(
                id=chunk_id,
                vector=vector,
                content=content,
                meta={"document_id": f"doc_{chunk_id}", "filename": "install.md", "heading": "安装"},
            )
            for (chunk_id, content), vector in zip(docs, vectors, strict=True)
        ],
    )
    return Retriever(embedder=embedder, store=store)


async def test_retriever_returns_ranked_hits_with_citation_source() -> None:
    retriever = await _retriever_with_kb(
        collection="kb_1",
        docs=[("c1", INSTALL_TEXT), ("c2", CONFIG_TEXT), ("c3", UNRELATED_TEXT)],
    )
    config = RetrievalConfig(kb_ids=("kb_1",), collection_names={"kb_1": "kb_1"}, top_k=3, score_threshold=0.3)

    chunks = await retriever.retrieve(INSTALL_TEXT, config)

    assert chunks
    assert chunks[0].chunk_id == "c1"
    assert chunks[0].document_id == "doc_c1"
    assert chunks[0].kb_id == "kb_1"
    assert chunks[0].score > 0.9
    assert "install.md#安装" in chunks[0].citation_source()
    assert "score=" in chunks[0].citation_source()


async def test_retriever_filters_by_score_threshold() -> None:
    retriever = await _retriever_with_kb(collection="kb_1", docs=[("c1", INSTALL_TEXT), ("c2", UNRELATED_TEXT)])
    strict = RetrievalConfig(kb_ids=("kb_1",), collection_names={"kb_1": "kb_1"}, top_k=3, score_threshold=0.5)

    assert [chunk.chunk_id for chunk in await retriever.retrieve(INSTALL_TEXT, strict)] == ["c1"]

    impossible = RetrievalConfig(kb_ids=("kb_1",), collection_names={"kb_1": "kb_1"}, top_k=3, score_threshold=1.1)
    assert await retriever.retrieve(INSTALL_TEXT, impossible) == []


async def test_retriever_merges_multiple_knowledge_bases() -> None:
    embedder = FakeEmbedder(dim=64)
    store = InMemoryVectorStore()
    vectors = await embedder.embed([INSTALL_TEXT, CONFIG_TEXT])
    await store.upsert(
        "kb_1",
        [VectorItem(id="c1", vector=vectors[0], content=INSTALL_TEXT, meta={"document_id": "d1"})],
    )
    await store.upsert(
        "kb_2",
        [VectorItem(id="c2", vector=vectors[1], content=CONFIG_TEXT, meta={"document_id": "d2"})],
    )
    retriever = Retriever(embedder=embedder, store=store)
    # 集合名约定 `kb_{kb_id}`（2.8）：kb_ids=("1","2") → 查 `kb_1` / `kb_2`
    config = RetrievalConfig(kb_ids=("1", "2"), top_k=5, score_threshold=0.0)

    chunks = await retriever.retrieve(INSTALL_TEXT, config)

    assert {chunk.kb_id for chunk in chunks} == {"1", "2"}
    assert {chunk.chunk_id for chunk in chunks} == {"c1", "c2"}


async def test_retriever_short_circuits_on_blank_query_or_empty_kb_list() -> None:
    retriever = await _retriever_with_kb(collection="kb_1", docs=[("c1", INSTALL_TEXT)])

    assert await retriever.retrieve("   ", RetrievalConfig(kb_ids=("kb_1",))) == []
    assert await retriever.retrieve(INSTALL_TEXT, RetrievalConfig(kb_ids=())) == []


async def test_retriever_applies_top_k_per_configuration() -> None:
    retriever = await _retriever_with_kb(
        collection="kb_1",
        docs=[("c1", INSTALL_TEXT), ("c2", CONFIG_TEXT), ("c3", UNRELATED_TEXT)],
    )
    config = RetrievalConfig(kb_ids=("kb_1",), collection_names={"kb_1": "kb_1"}, top_k=2, score_threshold=0.0)

    assert len(await retriever.retrieve(INSTALL_TEXT, config)) == 2


def test_build_context_blocks_and_citation_list_use_documented_formats() -> None:
    chunk = RetrievedChunk(
        chunk_id="c1",
        document_id="d1",
        kb_id="kb_1",
        score=0.834,
        content="安装步骤",
        meta={"filename": "install.md", "heading": "2.1 安装", "page": 2},
    )

    blocks = build_context_blocks([chunk])

    assert len(blocks) == 1
    assert blocks[0].startswith('<chunk id="c1" source="install.md#2.1 安装 page=2 score=0.83">')
    assert "安装步骤" in blocks[0]
    assert format_citation_list([chunk]).startswith("[1] source=install.md#2.1 安装 page=2 score=0.83")


def test_empty_retrieval_notice_text_is_fixed() -> None:
    assert EMPTY_RETRIEVAL_NOTICE == "当前知识库未检索到相关内容，请明确说明"
