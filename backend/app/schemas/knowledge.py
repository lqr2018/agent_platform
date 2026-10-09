"""知识库与文档的 DTO（详细设计 3.2.5 / 2.8 / 4.6）。

- `KnowledgeBaseCreate` 只暴露"用户可决定"的字段：`embedding_dim` / `collection_name` 由服务层
  按探测结果与约定（`kb_{id}`）填充，`vector_store_kind` 取 `Settings`，`rerank_enabled` 恒 false（2.8）；
- `splitter` 只接受 `recursive` / `markdown`（`token` 属迭代 E，SD-14② 不为未实现能力留入口）；
- `embedding_model` 在**有文档之后禁改**（2.8）—— 由服务层校验，DTO 层只做类型约束。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import UtcDatetime

SplitterName = Literal["recursive", "markdown"]
RetrieverKind = Literal["vector"]


class KnowledgeBaseCreate(BaseModel):
    """`POST /api/v1/knowledge-bases`（3.2.5：新建时探测 `embedding_dim`）。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    embedding_provider_id: str = Field(min_length=26, max_length=26)
    embedding_model: str = Field(min_length=1, max_length=120)
    splitter: SplitterName = "recursive"
    chunk_size: int = Field(default=800, ge=100, le=4000)
    chunk_overlap: int = Field(default=120, ge=0, le=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_overlap(self) -> KnowledgeBaseCreate:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class KnowledgeBaseUpdate(BaseModel):
    """`PATCH /api/v1/knowledge-bases/{id}`：只传需要改的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    embedding_model: str | None = Field(default=None, min_length=1, max_length=120)
    splitter: SplitterName | None = None
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class KnowledgeBaseRead(BaseModel):
    """KB 详情 / 列表项（含 `stats`，3.2.5）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    embedding_provider_id: str
    embedding_model: str
    embedding_dim: int = 0
    vector_store_kind: str = "chroma"
    collection_name: str
    splitter: str = "recursive"
    chunk_size: int = 800
    chunk_overlap: int = 120
    retriever_kind: str = "vector"
    top_k: int = 5
    score_threshold: float = 0.3
    rerank_enabled: bool = False
    stats: dict[str, object] = Field(default_factory=dict)
    status: str = "ready"
    created_at: UtcDatetime
    updated_at: UtcDatetime
    deleted_at: UtcDatetime | None = None


class DocumentRead(BaseModel):
    """文档详情 / 列表项（含摄取状态与失败原因，3.2.5）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kb_id: str
    filename: str
    source_type: str = "file"
    uri: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    checksum: str = ""
    status: str = "pending"
    chunk_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, object] = Field(default_factory=dict)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ChunkRead(BaseModel):
    """切片（3.2.5 的切片预览，分页返回）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    ordinal: int
    content: str
    token_count: int = 0
    vector_id: str = ""
    meta: dict[str, object] = Field(default_factory=dict)
    created_at: UtcDatetime


class QueryRequest(BaseModel):
    """`POST /knowledge-bases/{id}/query`（3.2.5：直接检索，前端调试与评测用）。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    kb_ids: list[str] | None = None
    """多 KB 试算（2.8："多 KB 结果合并后重排"）；缺省 = 只用路径里的那个 KB。"""


class QueryHitRead(BaseModel):
    """一条命中（含 4.6.3 的引用来源串，前端引用卡片直接渲染）。"""

    chunk_id: str
    document_id: str
    kb_id: str
    score: float
    content: str
    source: str
    meta: dict[str, object] = Field(default_factory=dict)


class QueryResultRead(BaseModel):
    """检索结果（`hit_count == 0` 时 `chunks` 为空数组，不报错 —— 4.6.3 的"未命中"是正常结果）。"""

    query: str
    kb_ids: list[str] = Field(default_factory=list)
    hit_count: int = 0
    chunks: list[QueryHitRead] = Field(default_factory=list)


class MaintenanceResultRead(BaseModel):
    """维护端点（`/maintenance` 下的对账与收敛）的返回。"""

    ok: bool
    detail: str = ""
    data: dict[str, object] = Field(default_factory=dict)
