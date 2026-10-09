"""`knowledge_bases` / `documents` / `chunks`（详细设计 2.8 / 7.6）。

三张表的关系（Phase 5 的数据侧）：

```text
knowledge_bases ──< documents ──< chunks ──┐
      ▲                                     └──> 向量集合 kb_{id}（chunks.id = 向量 id）
      └── agents.knowledge_base_ids（JSON 数组，服务层校验存在性，不建 FK）
```

- `chunks.id` 同时是**向量库的 id**（2.8：`vector_id` 冗余一列用于一致性校验）；
- `(document_id, ordinal)` 唯一：重复摄取同一文档时按序覆盖，不会产生两份切片；
- `documents.checksum` 是 SHA-256，同 KB 内相同即复用（4.6.2 的幂等），避免重复付 embedding 费用；
- `knowledge_bases` 用软删除（2.2：只有 `agents` / `knowledge_bases` 用 `deleted_at`），删除时
  还要删向量集合（服务层负责，先删向量再置 `deleted_at`，见 7.6 的风险与对策）；
- `stats` 是列表页展示用的冗余汇总（文档数 / 切片数 / 最近摄取时间），事实来源仍是
  `documents` / `chunks`，由服务层在摄取收敛时重算。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import DocumentStatus, KnowledgeBaseStatus
from app.db.base import Base, JSONDict, TimestampMixin, UUIDStr, now_utc

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120
"""切片参数默认值（2.8：按**字符**计）。"""

DEFAULT_RETRIEVE_TOP_K = 5
DEFAULT_SCORE_THRESHOLD = 0.3
"""检索默认值（2.8 / 4.6.1：`top_k=5`、余弦阈值 `0.3`）。"""

DEFAULT_SPLITTER = "recursive"
DEFAULT_RETRIEVER_KIND = "vector"
VECTOR_STORE_KIND_CHROMA = "chroma"
VECTOR_STORE_KIND_MEMORY = "memory"
"""SD-11：只实现 `chroma`（生产）与 `memory`（测试），不预留 qdrant。"""

DEFAULT_EMBEDDING_DIM = 0
"""`embedding_dim` 未探测前的占位（2.8：创建 KB 时探测并固化，0 表示未知）。"""

SOURCE_TYPE_FILE = "file"
SOURCE_TYPE_URL = "url"
SOURCE_TYPE_TEXT = "text"
"""`documents.source_type` 的三个取值（2.8）；MVP 只产生 `file`。"""


def collection_name_for(kb_id: str) -> str:
    """向量集合名约定 `kb_{id}`（2.8）。"""
    return f"kb_{kb_id}"


def default_kb_stats() -> dict[str, Any]:
    """`knowledge_bases.stats` 的初始值（2.8：文档数 / 切片数 / 最近摄取时间）。"""
    return {"document_count": 0, "chunk_count": 0, "last_ingested_at": None}


class KnowledgeBase(Base, TimestampMixin):
    """知识库（2.8）：一个 `embedding_model` + 一个向量集合 + 一套切片/检索参数。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[UUIDStr]
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    embedding_provider_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("model_providers.id", ondelete="RESTRICT"), nullable=False
    )
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=DEFAULT_EMBEDDING_DIM, nullable=False)
    vector_store_kind: Mapped[str] = mapped_column(String(16), default=VECTOR_STORE_KIND_CHROMA, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(64), nullable=False)
    splitter: Mapped[str] = mapped_column(String(16), default=DEFAULT_SPLITTER, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, default=DEFAULT_CHUNK_SIZE, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=DEFAULT_CHUNK_OVERLAP, nullable=False)
    retriever_kind: Mapped[str] = mapped_column(String(16), default=DEFAULT_RETRIEVER_KIND, nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, default=DEFAULT_RETRIEVE_TOP_K, nullable=False)
    score_threshold: Mapped[float] = mapped_column(Float, default=DEFAULT_SCORE_THRESHOLD, nullable=False)
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """恒为 false（2.8 / 4.6.1：`Reranker` 属迭代 E），列保留以固定接口。"""

    stats: Mapped[JSONDict] = mapped_column(default=default_kb_stats)
    status: Mapped[str] = mapped_column(String(16), default=str(KnowledgeBaseStatus.READY), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """软删除（2.2）：置位前先删向量集合。"""


class Document(Base, TimestampMixin):
    """知识库中的一份原始文档（2.8 / 4.6.2）：`status` 是摄取进度的**唯一判据**。"""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_kb_created", "kb_id", "created_at"),
        Index("ix_documents_kb_checksum", "kb_id", "checksum"),
    )

    id: Mapped[UUIDStr]
    kb_id: Mapped[str] = mapped_column(String(26), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), default=SOURCE_TYPE_FILE, nullable=False)
    uri: Mapped[str] = mapped_column(Text, default="", nullable=False)
    """落盘位置：`data/uploads/{kb_id}/{doc_id}`（2.8 的约定）。"""

    mime_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    """SHA-256（2.8）：同 KB 内相同则直接复用已有切片（4.6.2 的幂等）。"""

    status: Mapped[str] = mapped_column(String(16), default=str(DocumentStatus.PENDING), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[JSONDict]
    """形如 `{"uploaded_by": "local", "original_encoding": "utf-8"}`（2.8）。"""


class Chunk(Base):
    """文档的一个切片（2.8）：`id` 即向量库中的 id。"""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        Index("ix_chunks_kb_id", "kb_id"),
        Index("ix_chunks_document_id", "document_id"),
    )

    id: Mapped[UUIDStr]
    kb_id: Mapped[str] = mapped_column(String(26), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str] = mapped_column(String(26), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    """文档内顺序（从 0 开始）。"""

    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vector_id: Mapped[str] = mapped_column(String(26), default="", nullable=False)
    """与 `id` 相同（2.8：冗余一列，供 `verify_kb_index` 对账）。"""

    meta: Mapped[JSONDict]
    """引用展示所需：`{"page": 2, "heading": "2.1 安装", "source_url": "…"}`（2.8）。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
