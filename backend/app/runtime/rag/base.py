"""RAG 管线的基础类型与协议（详细设计 4.6.1 / 4.3.3 / 2.8）。

`runtime/**` 不 import `db/models` 与 `services`（1.2）：检索侧的 KB 参数由服务层装配成
`RetrievalConfig` 快照传进来，向量库与 embedding 只认协议。

三条协议对应 4.6.1 的表格：

| 环节 | 协议 | MVP 实现（模块） |
|---|---|---|
| Loader | `load(source) -> LoadedDocument` | `TextLoader` / `MarkdownLoader`（`loaders.py`） |
| Splitter | `split(document) -> list[TextChunk]` | `RecursiveSplitter` / `MarkdownSplitter`（`splitters.py`） |
| Embedder | `embed(texts) -> list[list[float]]` | `OpenAICompatibleEmbedder` / `FakeEmbedder`（`embedders.py`） |
| VectorStore | 4.3.3 的三个方法 + `drop` / `count` | `ChromaVectorStore` / `InMemoryVectorStore`（`vectorstores.py`） |
| Retriever | `retrieve(...) -> list[RetrievedChunk]` | `retriever.py`（多 KB 合并；rerank 属迭代 E） |

**与 4.3.3 的偏差（仅命名）**：文档把向量库协议叫 `MemoryBackend`（因为长期记忆也要用它）。
Phase 5 先行落地 RAG，若真叫 `MemoryBackend` 会让"知识库检索"这条链路读起来像记忆语义，
故本实现命名为 `VectorStore`；方法签名与文档逐字一致，Phase 4 直接复用同一个类。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.core.ids import new_ulid

DEFAULT_FAKE_EMBEDDING_DIM = 64
"""`FakeEmbedder` 的向量维度（4.1.3：确定性、可离线跑通 DoD）。"""


@dataclass(frozen=True, slots=True)
class LoaderSource:
    """`Loader.load()` 的入参（2.8 的 `documents` 行在运行时的最小投影）。"""

    path: Path
    filename: str
    mime_type: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    """4.6.1 的 `Document = {text, meta}`。

    `meta` 由 Loader 决定：`MarkdownLoader` 放标题层级、编码探测结果放 `original_encoding`
    （2.8 的 `documents.meta`），并透传到每个切片的 `meta`（引用展示需要）。
    """

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TextChunk:
    """切片结果（2.8 的 `chunks` 行在运行时的形态，`id` 在落库/入库时生成）。"""

    ordinal: int
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0


@dataclass(frozen=True, slots=True)
class VectorItem:
    """写入向量库的一条记录（4.3.3 的 `VectorItem`）。

    `content` 与 `meta` 一并存进向量库：Chroma 的 `documents` / `metadatas` 能在不查 SQLite 的
    情况下渲染引用卡片；`id` 用 `chunks.id`（2.8：`chunks.id` = 向量 id）。
    """

    id: str
    vector: list[float]
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorHit:
    """向量检索的一条命中（4.3.3 的 `VectorHit`）：`score` 为余弦相似度（越大越相关）。"""

    id: str
    score: float
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """检索结果的统一返回结构（2.8：被 Agent 与前端共用）。"""

    chunk_id: str
    document_id: str
    kb_id: str
    score: float
    content: str
    meta: dict[str, Any] = field(default_factory=dict)

    def citation_source(self) -> str:
        """4.6.3 的引用来源串：`install.md#2.1 page=2 score=0.83`（注入 `<chunk source=...>`）。"""
        parts = [str(self.meta.get("filename") or self.document_id)]
        heading = self.meta.get("heading")
        if heading:
            parts.append(f"#{heading}")
        source = "".join(parts)
        if "page" in self.meta:
            source += f" page={self.meta['page']}"
        return f"{source} score={self.score:.2f}"


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """一次检索的参数快照（服务层从 `knowledge_bases` 行装配；runtime 不碰 ORM）。"""

    kb_ids: tuple[str, ...] = ()
    collection_names: Mapping[str, str] = field(default_factory=dict)
    top_k: int = 5
    score_threshold: float = 0.3


class Loader(Protocol):
    """4.6.1 的 Loader 契约（单文件 ≤ 10 MB 由服务层按 `UPLOAD_MAX_BYTES` 把关）。"""

    def supports(self, source: LoaderSource) -> bool: ...

    def load(self, source: LoaderSource) -> LoadedDocument: ...


class Splitter(Protocol):
    """4.6.1 的 Splitter 契约。"""

    def split(self, document: LoadedDocument) -> list[TextChunk]: ...


class Embedder(Protocol):
    """4.6.1 的 Embedder 契约（`model` 在构造时固化，与 KB 的 `embedding_model` 一致）。"""

    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    """4.3.3 的向量后端契约（`upsert` / `query` / `delete` + 运维用的 `drop` / `count`）。"""

    async def upsert(self, collection: str, items: list[VectorItem]) -> None: ...

    async def query(self, collection: str, vector: list[float], top_k: int) -> list[VectorHit]: ...

    async def delete(self, collection: str, ids: list[str]) -> None: ...

    async def drop(self, collection: str) -> None: ...

    async def count(self, collection: str) -> int:
        """`verify_kb_index` 的对账用（7.6 的风险与对策）。"""
        ...


def new_vector_id() -> str:
    """切片 / 向量的主键（26 位 ULID，0.2.1）。"""
    return new_ulid()
