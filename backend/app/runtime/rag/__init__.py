"""RAG 运行时包（详细设计 4.6 / 7.6）。

按 7.6 的交付物**平铺**这些模块（1.1 目录树画的是含 Backlog 文件的"子包目标态"；本阶段按 7.6 落点
实现，迭代 E 要加 pdf/web/token 时再拆包 —— 附录 F v1.16 记录了这一取舍）：

| 模块 | 内容 |
|---|---|
| `base.py` | 共享类型 + 四条协议（`Loader` / `Splitter` / `Embedder` / `VectorStore`） |
| `loaders.py` | `TextLoader` / `MarkdownLoader` / `LoaderRegistry` |
| `splitters.py` | `RecursiveSplitter` / `MarkdownSplitter` / `build_splitter` |
| `embedders.py` | `OpenAICompatibleEmbedder` / `FakeEmbedder` / `build_embedder` / `probe_embedding_dim` |
| `vectorstores.py` | `ChromaVectorStore` / `InMemoryVectorStore` / `build_vector_store` |
| `retriever.py` | `Retriever` / `build_context_blocks` / `EMPTY_RETRIEVAL_NOTICE` |
| `pipeline.py` | `IngestPipeline` / `IngestResult`（4.6.2 三阶段的纯计算编排） |

`rerank.py` **不存在**（4.6.1：rerank 属迭代 E，插点在 `Retriever._rerank`）。
"""

from __future__ import annotations

from app.runtime.rag.base import (
    DEFAULT_FAKE_EMBEDDING_DIM,
    Embedder,
    LoadedDocument,
    Loader,
    LoaderSource,
    RetrievalConfig,
    RetrievedChunk,
    Splitter,
    TextChunk,
    VectorHit,
    VectorItem,
    VectorStore,
)
from app.runtime.rag.embedders import (
    FakeEmbedder,
    OpenAICompatibleEmbedder,
    build_embedder,
    probe_embedding_dim,
)
from app.runtime.rag.loaders import LoaderRegistry, MarkdownLoader, TextLoader
from app.runtime.rag.pipeline import IngestPipeline, IngestResult
from app.runtime.rag.retriever import (
    EMPTY_RETRIEVAL_NOTICE,
    Retriever,
    build_context_blocks,
    format_citation_list,
)
from app.runtime.rag.splitters import (
    MarkdownSplitter,
    RecursiveSplitter,
    build_splitter,
    estimate_tokens,
)
from app.runtime.rag.vectorstores import (
    ChromaVectorStore,
    InMemoryVectorStore,
    build_vector_store,
    cosine_similarity,
    reset_memory_vector_store,
)

__all__ = [
    "DEFAULT_FAKE_EMBEDDING_DIM",
    "EMPTY_RETRIEVAL_NOTICE",
    "ChromaVectorStore",
    "Embedder",
    "FakeEmbedder",
    "InMemoryVectorStore",
    "IngestPipeline",
    "IngestResult",
    "LoadedDocument",
    "Loader",
    "LoaderRegistry",
    "LoaderSource",
    "MarkdownLoader",
    "MarkdownSplitter",
    "OpenAICompatibleEmbedder",
    "RecursiveSplitter",
    "RetrievalConfig",
    "RetrievedChunk",
    "Retriever",
    "Splitter",
    "TextChunk",
    "TextLoader",
    "VectorHit",
    "VectorItem",
    "VectorStore",
    "build_context_blocks",
    "build_embedder",
    "build_splitter",
    "build_vector_store",
    "cosine_similarity",
    "estimate_tokens",
    "format_citation_list",
    "probe_embedding_dim",
    "reset_memory_vector_store",
]
