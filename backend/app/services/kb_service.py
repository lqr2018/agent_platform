"""知识库服务（详细设计 2.8 / 4.6 / 7.6）。

职责边界（1.2：api 只做协议转换、runtime 不碰 ORM）：

- **KB CRUD**：建 KB 时探测 `embedding_dim` 并固化（2.8）；删除时先删向量集合再软删（7.6 风险）；
- **摄取编排**（4.6.2 状态机）：`pending → parsing → chunking → embedding → ready`，任一步失败写
  `failed` + `error_code` / `error_message`，可 `reingest`；重活交给 `task_runner`（进程内队列）；
- **检索**：把 KB 行的参数装配成 `RetrievalConfig`（runtime 快照）；
- **运维**：`verify_kb_index`（DB chunk 数 vs 向量条数对账）、`converge_interrupted_documents()`
  （启动时把 in-flight 状态置 `failed`，见 `task_runner` 的模块 docstring 第 2 条）。

三条不变量：

1. `documents.status` 是摄取进度的**唯一判据**（4.6.2）—— 落盘、`chunks` 行与向量都围绕它推进；
2. **摄取先写库、后写向量**（失败可 `reingest`）；**删除相反：先删向量、再删行**（7.6 风险与对策）；
3. 同 KB 内 `checksum` 相同的文档**不重复 embedding**（4.6.2 幂等），直接复用已有切片。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings, get_settings
from app.core.enums import DocumentStatus, KnowledgeBaseStatus
from app.core.errors import (
    AppError,
    ConfigInvalidError,
    ConflictError,
    ErrorCode,
    KnowledgeBaseDocumentNotFoundError,
    KnowledgeBaseEmptyIndexError,
    KnowledgeBaseIngestFailedError,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnsupportedFormatError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.base import now_utc
from app.db.models import Chunk, Document, KnowledgeBase, ModelProvider
from app.db.models.knowledge import DEFAULT_RETRIEVE_TOP_K, DEFAULT_SCORE_THRESHOLD, collection_name_for
from app.db.session import get_sessionmaker
from app.runtime.llm.registry import LLMRegistry
from app.runtime.rag import (
    Embedder,
    IngestPipeline,
    LoaderRegistry,
    LoaderSource,
    RetrievalConfig,
    RetrievedChunk,
    Retriever,
    TextChunk,
    VectorItem,
    build_embedder,
    build_vector_store,
    probe_embedding_dim,
)
from app.schemas.knowledge import KnowledgeBaseCreate, KnowledgeBaseUpdate, QueryRequest
from app.services import model_provider_service
from app.services.task_runner import get_task_runner

logger = get_logger(__name__)

INGEST_ACTIVE_STATUSES: tuple[str, ...] = (
    str(DocumentStatus.PARSING),
    str(DocumentStatus.CHUNKING),
    str(DocumentStatus.EMBEDDING),
)
"""4.6.2 里"进程重启后无法继续"的中间态（被 `converge_interrupted_documents` 收敛为 `failed`）。"""

MAX_CHUNK_PAGE_SIZE = 200
"""切片预览的分页上限（`GET /{doc_id}/chunks`）。"""


# ---- KB CRUD ----


async def list_knowledge_bases(
    session: AsyncSession,
    *,
    q: str | None = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> Sequence[KnowledgeBase]:
    """KB 列表（2.8；默认过滤软删除）。"""
    statement = select(KnowledgeBase)
    if not include_deleted:
        statement = statement.where(KnowledgeBase.deleted_at.is_(None))
    if q:
        statement = statement.where(KnowledgeBase.name.like(f"%{q}%"))
    statement = statement.order_by(KnowledgeBase.created_at.desc()).limit(limit)
    return (await session.execute(statement)).scalars().all()


async def get_knowledge_base(session: AsyncSession, kb_id: str, *, include_deleted: bool = False) -> KnowledgeBase:
    """取 KB（不存在 / 已软删 → `KB_NOT_FOUND`）。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or (kb.deleted_at is not None and not include_deleted):
        raise KnowledgeBaseNotFoundError(f"Knowledge base '{kb_id}' not found", details={"kb_id": kb_id})
    return kb


async def get_knowledge_bases_by_ids(session: AsyncSession, kb_ids: Sequence[str]) -> list[KnowledgeBase]:
    """按 id 批量取（Agent 绑定时校验用；缺失的 id 由调用方决定怎么报）。"""
    if not kb_ids:
        return []
    statement = select(KnowledgeBase).where(KnowledgeBase.id.in_(list(kb_ids)), KnowledgeBase.deleted_at.is_(None))
    rows = (await session.execute(statement)).scalars().all()
    by_id = {row.id: row for row in rows}
    return [by_id[kb_id] for kb_id in kb_ids if kb_id in by_id]


async def create_knowledge_base(
    session: AsyncSession,
    data: KnowledgeBaseCreate,
    *,
    settings: Settings | None = None,
) -> KnowledgeBase:
    """新建 KB：校验 Provider / 模型 → **探测 `embedding_dim`** → 落库（2.8 / 3.2.5）。"""
    resolved = settings or get_settings()
    provider = await model_provider_service.get_provider(session, data.embedding_provider_id)
    _validate_embedding_model(provider, data.embedding_model)

    existing = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == data.name))
    if existing is not None:
        raise ConflictError(
            f"Knowledge base '{data.name}' already exists",
            details={"name": data.name, "kb_id": existing.id},
        )

    embedder = build_kb_embedder(provider, model=data.embedding_model, settings=resolved)
    embedding_dim = await probe_embedding_dim(embedder)

    kb_id = ids.new_ulid()
    kb = KnowledgeBase(
        id=kb_id,
        name=data.name,
        description=data.description,
        embedding_provider_id=data.embedding_provider_id,
        embedding_model=data.embedding_model,
        embedding_dim=embedding_dim,
        vector_store_kind=resolved.vector_store_kind,
        collection_name=collection_name_for(kb_id),
        splitter=data.splitter,
        chunk_size=data.chunk_size,
        chunk_overlap=data.chunk_overlap,
        retriever_kind="vector",
        top_k=data.top_k,
        score_threshold=data.score_threshold,
        rerank_enabled=False,
        stats={"document_count": 0, "chunk_count": 0, "last_ingested_at": None},
        status=str(KnowledgeBaseStatus.READY),
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    logger.info("kb.created", kb_id=kb.id, name=kb.name, embedding_dim=embedding_dim)
    return kb


async def update_knowledge_base(
    session: AsyncSession,
    kb_id: str,
    data: KnowledgeBaseUpdate,
) -> KnowledgeBase:
    """更新 KB（2.8：`embedding_model` **有文档后禁改** —— 新旧向量不在同一空间）。"""
    kb = await get_knowledge_base(session, kb_id)
    fields = data.model_dump(exclude_unset=True)
    if "embedding_model" in fields and fields["embedding_model"] != kb.embedding_model:
        document_count = await session.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id))
        if document_count:
            raise ValidationError(
                "embedding_model cannot be changed after documents are uploaded; create a new knowledge base",
                details={"kb_id": kb_id, "document_count": int(document_count or 0)},
            )
        kb.embedding_dim = 0  # 换模型后维度需重新探测（下次创建文档前由服务层补齐）
    for key, value in fields.items():
        setattr(kb, key, value)
    _validate_chunk_params(kb.chunk_size, kb.chunk_overlap)
    kb.updated_at = now_utc()
    await session.commit()
    await session.refresh(kb)
    return kb


async def delete_knowledge_base(
    session: AsyncSession,
    kb_id: str,
    *,
    settings: Settings | None = None,
) -> None:
    """软删除 KB + 删除向量集合（2.2 / 7.6：**先删向量、再置 `deleted_at`**）。

    `documents` / `chunks` 行保留（软删语义：审计与复现仍可查），但 KB 已从列表与检索中消失。
    """
    resolved = settings or get_settings()
    kb = await get_knowledge_base(session, kb_id)
    store = build_vector_store(kind=kb.vector_store_kind, chroma_dir=resolved.chroma_path)
    await store.drop(kb.collection_name)
    kb.deleted_at = now_utc()
    kb.updated_at = now_utc()
    await session.commit()
    logger.info("kb.deleted", kb_id=kb.id, collection=kb.collection_name)


def build_kb_embedder(provider: ModelProvider, *, model: str, settings: Settings) -> Embedder:
    """按 Provider 行装配 Embedder（`kind=fake` 走确定性实现，受 `EMBED_ALLOW_FAKE` 闸门约束）。"""
    if provider.kind == "fake":
        return build_embedder(
            provider=None,
            model=model,
            batch_size=settings.embedding_batch_size,
            max_retries=settings.llm_max_retries,
            allow_fake=settings.embed_allow_fake,
        )
    llm = LLMRegistry(settings).create(model_provider_service.to_config(provider, settings=settings))
    return build_embedder(
        provider=llm,
        model=model,
        batch_size=settings.embedding_batch_size,
        max_retries=settings.llm_max_retries,
        allow_fake=settings.embed_allow_fake,
    )


def _validate_embedding_model(provider: ModelProvider, model: str) -> None:
    """模型必须在 Provider 的白名单里（2.3 的 `models[]`）；空白名单表示"不限制"。"""
    names = tuple(str(item.get("name")) for item in (provider.models or []) if item.get("name"))
    if names and model not in names:
        raise ValidationError(
            f"Model '{model}' is not in provider '{provider.name}' whitelist",
            details={"provider_id": provider.id, "model": model, "available": list(names)},
        )


def _validate_chunk_params(chunk_size: int, chunk_overlap: int) -> None:
    if chunk_overlap >= chunk_size:
        raise ValidationError(
            "chunk_overlap must be smaller than chunk_size",
            details={"chunk_size": chunk_size, "chunk_overlap": chunk_overlap},
        )


# ---- 文档上传（4.6.2 的入口） ----


def upload_path(settings: Settings, *, kb_id: str, doc_id: str) -> Path:
    """2.8 的落盘约定 `data/uploads/{kb_id}/{doc_id}`。

    文件名**不进路径**（只有 ULID）：上传的文件名是用户可控的，用它拼路径就是路径穿越的入口
    （`../../app/main.py`）；原文件名只存在 `documents.filename` 里用于展示与引用。
    """
    return settings.data_path / "uploads" / kb_id / doc_id


async def create_document(
    session: AsyncSession,
    kb_id: str,
    *,
    filename: str,
    content: bytes,
    mime_type: str = "",
    settings: Settings | None = None,
) -> Document:
    """上传并排入摄取队列（3.2.5：返回 `202` + `pending` 文档）。

    顺序（4.6.2 + 模块 docstring 的不变量 2）：**先落库拿 id → 再落盘 → 最后入队**。
    任何一步失败都把行收敛为 `failed`，不会留下"永远 pending"的幽灵文档。
    """
    resolved = settings or get_settings()
    await get_knowledge_base(session, kb_id)
    if not content:
        raise ValidationError("Uploaded file is empty", details={"filename": filename})
    if len(content) > resolved.upload_max_bytes:
        raise KnowledgeBaseUnsupportedFormatError(
            f"File exceeds the upload limit of {resolved.upload_max_bytes} bytes",
            details={"filename": filename, "size_bytes": len(content), "limit": resolved.upload_max_bytes},
        )

    doc_id = ids.new_ulid()
    source = LoaderSource(
        path=upload_path(resolved, kb_id=kb_id, doc_id=doc_id),
        filename=filename,
        mime_type=mime_type,
    )
    LoaderRegistry().for_source(source)  # 不支持的后缀在**入队之前**就 422（别让它进 failed）

    checksum = hashlib.sha256(content).hexdigest()
    reused_from = await _find_reusable_document(session, kb_id=kb_id, checksum=checksum)
    document = Document(
        id=doc_id,
        kb_id=kb_id,
        filename=filename,
        source_type="file",
        uri=f"data/uploads/{kb_id}/{doc_id}",
        mime_type=mime_type,
        size_bytes=len(content),
        checksum=checksum,
        status=str(DocumentStatus.READY if reused_from is not None else DocumentStatus.PENDING),
        chunk_count=int(reused_from.chunk_count) if reused_from is not None else 0,
        meta={
            "uploaded_by": "local",
            "filename": filename,
            **({"reused_from_document_id": reused_from.id} if reused_from is not None else {}),
        },
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)

    path = upload_path(resolved, kb_id=kb_id, doc_id=doc_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except OSError as exc:
        await fail_document(
            session,
            document,
            KnowledgeBaseIngestFailedError(
                f"Failed to store the uploaded file: {exc}",
                details={"stage": "store", "filename": filename},
            ),
        )
        raise

    kb = await get_knowledge_base(session, kb_id)
    await refresh_kb_state(session, kb)
    if reused_from is None:
        await get_task_runner(resolved).submit(lambda: ingest_document(doc_id, settings=resolved))
        logger.info("kb.document_queued", kb_id=kb_id, document_id=doc_id, size_bytes=len(content))
    else:
        logger.info("kb.document_reused", kb_id=kb_id, document_id=doc_id, reused_from=reused_from.id)
    return document


async def _find_reusable_document(session: AsyncSession, *, kb_id: str, checksum: str) -> Document | None:
    """4.6.2 的幂等：同 KB 内已有**同样 checksum 且 ready** 的文档 → 复用它的切片。"""
    if not checksum:
        return None
    statement = (
        select(Document)
        .where(
            Document.kb_id == kb_id,
            Document.checksum == checksum,
            Document.status == str(DocumentStatus.READY),
        )
        .order_by(Document.created_at.asc())
        .limit(1)
    )
    return await session.scalar(statement)


# ---- 摄取状态机（4.6.2） ----


async def set_document_status(session: AsyncSession, document: Document, status: DocumentStatus) -> None:
    """推进状态并**立刻提交**：前端按 2s 轮询看进度（4.6.2 的"进度可见"）。"""
    document.status = str(status)
    document.updated_at = now_utc()
    await session.commit()


async def fail_document(session: AsyncSession, document: Document, error: AppError) -> None:
    """把文档收敛为 `failed` 并记录 `error_code` / `error_message`（2.8），同时刷新 KB 状态。"""
    document.status = str(DocumentStatus.FAILED)
    document.error_code = str(error.code)
    document.error_message = error.message
    document.updated_at = now_utc()
    await session.commit()
    kb = await session.get(KnowledgeBase, document.kb_id)
    if kb is not None:
        await refresh_kb_state(session, kb)


async def refresh_kb_state(session: AsyncSession, kb: KnowledgeBase) -> None:
    """重算 `knowledge_bases.stats` 与 `status`（2.8）：`ingesting` / `ready` / `error`。"""
    rows = (await session.execute(select(Document.status, Document.chunk_count).where(Document.kb_id == kb.id))).all()
    statuses = {str(row[0]) for row in rows}
    if statuses & {*INGEST_ACTIVE_STATUSES, str(DocumentStatus.PENDING)}:
        kb.status = str(KnowledgeBaseStatus.INGESTING)
    elif statuses and statuses <= {str(DocumentStatus.FAILED)}:
        kb.status = str(KnowledgeBaseStatus.ERROR)
    else:
        kb.status = str(KnowledgeBaseStatus.READY)

    stats = dict(kb.stats or {})
    stats["document_count"] = len(rows)
    stats["chunk_count"] = sum(int(row[1] or 0) for row in rows)
    last_ready = await session.scalar(
        select(func.max(Document.updated_at)).where(
            Document.kb_id == kb.id,
            Document.status == str(DocumentStatus.READY),
        )
    )
    if last_ready is not None:
        stats["last_ingested_at"] = last_ready.isoformat()
    kb.stats = stats
    kb.updated_at = now_utc()
    await session.commit()


async def ingest_document(  # noqa: PLR0911 - 每个 return 都是一个明确的终态字符串
    document_id: str, *, settings: Settings | None = None
) -> str:
    """后台任务：跑完 `pending → parsing → chunking → embedding → ready`（4.6.2）。

    返回终态字符串（`ready` / `failed` / `missing`），便于测试与日志直接断言。
    用**新 session**：任务生命周期与请求无关（与 `chat_service._execute` 同一约定）。
    """
    resolved = settings or get_settings()
    async with get_sessionmaker()() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.warning("kb.ingest_missing_document", document_id=document_id)
            return "missing"
        kb = await session.get(KnowledgeBase, document.kb_id)
        if kb is None:
            await fail_document(
                session,
                document,
                KnowledgeBaseIngestFailedError("Knowledge base disappeared", details={"stage": "parsing"}),
            )
            return "failed"
        provider = await session.get(ModelProvider, kb.embedding_provider_id)
        if provider is None:
            await fail_document(
                session,
                document,
                KnowledgeBaseIngestFailedError("Embedding provider disappeared", details={"stage": "embedding"}),
            )
            return "failed"

        embedder = build_kb_embedder(provider, model=kb.embedding_model, settings=resolved)
        store = build_vector_store(kind=kb.vector_store_kind, chroma_dir=resolved.chroma_path)
        source = LoaderSource(
            path=upload_path(resolved, kb_id=kb.id, doc_id=document.id),
            filename=document.filename,
            mime_type=document.mime_type,
        )
        pipeline = IngestPipeline()

        try:
            await set_document_status(session, document, DocumentStatus.PARSING)
            loaded = pipeline.load(source)
            await set_document_status(session, document, DocumentStatus.CHUNKING)
            chunks = pipeline.split(
                loaded, splitter=kb.splitter, chunk_size=kb.chunk_size, chunk_overlap=kb.chunk_overlap
            )
            await set_document_status(session, document, DocumentStatus.EMBEDDING)
            vectors = await pipeline.embed(chunks, embedder=embedder)
        except AppError as exc:
            await fail_document(session, document, exc)
            logger.warning("kb.ingest_failed", document_id=document.id, error_code=str(exc.code))
            return "failed"
        except Exception as exc:  # 兜底：任何异常都不许把文档留在中间态
            await fail_document(
                session,
                document,
                KnowledgeBaseIngestFailedError(str(exc) or "Ingestion failed", details={"stage": "unknown"}),
            )
            logger.error("kb.ingest_crashed", document_id=document.id, exc_info=True)
            return "failed"

        try:
            # 不变量 2：**先写 `chunks` 行、后写向量**（失败可 `reingest`）
            items = await persist_chunks(session, document=document, kb=kb, chunks=chunks, vectors=vectors)
            await store.upsert(kb.collection_name, items)
        except AppError as exc:
            await fail_document(session, document, exc)
            return "failed"

        document.status = str(DocumentStatus.READY)
        document.chunk_count = len(chunks)
        document.error_code = None
        document.error_message = None
        document.updated_at = now_utc()
        await session.commit()
        await refresh_kb_state(session, kb)
        logger.info("kb.ingest_ready", document_id=document.id, chunks=len(chunks))
        return "ready"


async def persist_chunks(
    session: AsyncSession,
    *,
    document: Document,
    kb: KnowledgeBase,
    chunks: Sequence[TextChunk],
    vectors: Sequence[Sequence[float]],
) -> list[VectorItem]:
    """写 `chunks` 行并返回等长的 `VectorItem`（**切片 id = 向量 id**，2.8）。

    - reingest 前先删该文档的旧切片（`(document_id, ordinal)` 是 UNIQUE）；
    - 用 `insert()` 批量写：10 MB 文档可能有数千行，逐条 `session.add` 在 SQLite 上会明显变慢；
    - 全空白文档得到 0 行 0 向量，仍然算 `ready`（4.6.2 的 `failed` 必须是**真失败**）。
    """
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    items: list[VectorItem] = []
    rows: list[dict[str, object]] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk_id = ids.new_ulid()
        meta: dict[str, object] = {**dict(chunk.meta), "document_id": document.id, "kb_id": kb.id}
        rows.append(
            {
                "id": chunk_id,
                "kb_id": kb.id,
                "document_id": document.id,
                "ordinal": chunk.ordinal,
                "content": chunk.content,
                "token_count": chunk.token_count,
                "vector_id": chunk_id,
                "meta": meta,
                "created_at": now_utc(),
            }
        )
        items.append(VectorItem(id=chunk_id, vector=list(vector), content=chunk.content, meta=meta))
    if rows:
        await session.execute(insert(Chunk), rows)
    await session.commit()
    return items


# ---- 文档查询与运维 ----


async def list_documents(
    session: AsyncSession,
    kb_id: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> Sequence[Document]:
    """文档列表（3.2.5；含摄取状态，前端 2s 轮询它）。"""
    await get_knowledge_base(session, kb_id)
    statement = select(Document).where(Document.kb_id == kb_id)
    if status:
        statement = statement.where(Document.status == status)
    statement = statement.order_by(Document.created_at.desc()).limit(limit)
    return (await session.execute(statement)).scalars().all()


async def get_document(session: AsyncSession, kb_id: str, doc_id: str) -> Document:
    """取文档（不存在 / 不属于该 KB → `KB_DOCUMENT_NOT_FOUND`）。"""
    document = await session.get(Document, doc_id)
    if document is None or document.kb_id != kb_id:
        raise KnowledgeBaseDocumentNotFoundError(
            f"Document '{doc_id}' not found in knowledge base '{kb_id}'",
            details={"kb_id": kb_id, "document_id": doc_id},
        )
    return document


async def list_chunks(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
    *,
    offset: int = 0,
    limit: int = 50,
) -> Sequence[Chunk]:
    """切片预览（3.2.5：分页返回，按 `ordinal` 升序）。"""
    await get_document(session, kb_id, doc_id)
    statement = (
        select(Chunk)
        .where(Chunk.document_id == doc_id)
        .order_by(Chunk.ordinal.asc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), MAX_CHUNK_PAGE_SIZE))
    )
    return (await session.execute(statement)).scalars().all()


async def delete_document(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
    *,
    settings: Settings | None = None,
) -> None:
    """删除文档 + 切片 + 向量 + 源文件（3.2.5；7.6 的 DoD：**删除后不再命中**）。

    顺序（不变量 2 的删除侧）：先删向量 → 再删 `chunks` / `documents` 行 → 最后删源文件。
    """
    resolved = settings or get_settings()
    kb = await get_knowledge_base(session, kb_id)
    document = await get_document(session, kb_id, doc_id)
    await drop_document_vectors(session, kb=kb, document=document, settings=resolved)
    await session.delete(document)
    await session.commit()

    path = upload_path(resolved, kb_id=kb_id, doc_id=doc_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 删源文件失败不影响检索一致性
        logger.warning("kb.upload_unlink_failed", kb_id=kb_id, document_id=doc_id, exc_info=True)
    await refresh_kb_state(session, kb)
    logger.info("kb.document_deleted", kb_id=kb_id, document_id=doc_id)


async def drop_document_vectors(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    document: Document,
    settings: Settings,
) -> None:
    """删掉该文档的所有向量与 `chunks` 行（`documents` 行是否删除由调用方决定）。"""
    vector_ids = [
        str(row)
        for row in (await session.scalars(select(Chunk.vector_id).where(Chunk.document_id == document.id))).all()
        if row
    ]
    if vector_ids:
        store = build_vector_store(kind=kb.vector_store_kind, chroma_dir=settings.chroma_path)
        await store.delete(kb.collection_name, vector_ids)
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await session.commit()


async def reingest_document(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
    *,
    settings: Settings | None = None,
) -> Document:
    """重新摄取（3.2.5：改 chunk 参数后用）：清旧切片与向量 → 重置 `pending` → 重新入队。"""
    resolved = settings or get_settings()
    kb = await get_knowledge_base(session, kb_id)
    document = await get_document(session, kb_id, doc_id)
    await drop_document_vectors(session, kb=kb, document=document, settings=resolved)

    document.status = str(DocumentStatus.PENDING)
    document.chunk_count = 0
    document.error_code = None
    document.error_message = None
    document.updated_at = now_utc()
    await session.commit()
    await refresh_kb_state(session, kb)
    await get_task_runner(resolved).submit(lambda: ingest_document(document.id, settings=resolved))
    logger.info("kb.document_reingest_queued", kb_id=kb_id, document_id=doc_id)
    return document


async def converge_interrupted_documents(session: AsyncSession) -> int:
    """启动收尾（6.3 第 3 条的同款逻辑）：把上次进程留下的中间态文档收敛为 `failed`。

    没有这一步，进程崩溃后文档会**永远**停在 `parsing` / `chunking` / `embedding`，
    前端轮询只能看到一个不会变的状态（`task_runner` 模块 docstring 的第 2 条）。
    """
    rows = (await session.scalars(select(Document).where(Document.status.in_(list(INGEST_ACTIVE_STATUSES))))).all()
    if not rows:
        return 0
    now = now_utc()
    for document in rows:
        document.status = str(DocumentStatus.FAILED)
        document.error_code = str(ErrorCode.KB_INGEST_FAILED)
        document.error_message = "Ingestion was interrupted by a process restart; reingest to retry"
        document.updated_at = now
    await session.commit()
    for kb_id in {document.kb_id for document in rows}:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is not None:
            await refresh_kb_state(session, kb)
    logger.info("kb.interrupted_documents_converged", count=len(rows))
    return len(rows)


async def verify_kb_index(
    session: AsyncSession,
    kb_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    """对账（7.6 的风险与对策）：`chunks` 行数 vs 向量集合条数。"""
    resolved = settings or get_settings()
    kb = await get_knowledge_base(session, kb_id)
    db_chunks = int(await session.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb.id)) or 0)
    store = build_vector_store(kind=kb.vector_store_kind, chroma_dir=resolved.chroma_path)
    vector_items = await store.count(kb.collection_name)
    return {
        "kb_id": kb.id,
        "collection": kb.collection_name,
        "db_chunks": db_chunks,
        "vector_items": vector_items,
        "ok": db_chunks == vector_items,
    }


async def vector_store_health(settings: Settings) -> tuple[bool, str]:
    """`/readyz` 的向量库检查（6.3 第 4 条）：`chroma` 走真实 heartbeat，`memory` 恒 ok。"""
    if settings.vector_store_kind == "memory":
        return True, "memory (in-process)"
    path = settings.chroma_path
    if not path.exists():
        return False, f"directory missing: {path}"
    if not os.access(path, os.W_OK):
        return False, f"directory not writable: {path}"
    try:
        await asyncio.to_thread(_chroma_heartbeat, str(path))
    except ConfigInvalidError as exc:  # 缺 chromadb → 就绪探针明确失败（而不是伪装成 ok）
        return False, exc.message
    except Exception as exc:  # pragma: no cover - 依赖内部异常
        return False, f"chroma heartbeat failed: {type(exc).__name__}"
    return True, f"chroma ok: {path}"


@lru_cache(maxsize=4)
def _chroma_client(path: str) -> object:
    """按目录缓存 `PersistentClient`（就绪探针每 2s 调一次，不能每次新建 client）。"""
    import chromadb  # noqa: PLC0415 - 与 `ChromaVectorStore` 同一约定：惰性 import

    return chromadb.PersistentClient(path=path)


def _chroma_heartbeat(path: str) -> None:
    client = _chroma_client(path)
    heartbeat = getattr(client, "heartbeat", None)
    if callable(heartbeat):
        heartbeat()


# ---- 检索（4.6.1 / 4.6.3） ----


def retrieval_config(
    kbs: Sequence[KnowledgeBase],
    *,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> RetrievalConfig:
    """把 KB 行的参数装配成 runtime 快照（多 KB 时 `top_k` 取最大、阈值取最小）。"""
    thresholds = [kb.score_threshold for kb in kbs]
    return RetrievalConfig(
        kb_ids=tuple(kb.id for kb in kbs),
        collection_names={kb.id: kb.collection_name for kb in kbs},
        top_k=top_k or max((kb.top_k for kb in kbs), default=DEFAULT_RETRIEVE_TOP_K),
        score_threshold=(
            score_threshold
            if score_threshold is not None
            else (min(thresholds) if thresholds else DEFAULT_SCORE_THRESHOLD)
        ),
    )


async def retrieve(
    session: AsyncSession,
    kb_ids: Sequence[str],
    query: str,
    *,
    top_k: int | None = None,
    score_threshold: float | None = None,
    settings: Settings | None = None,
) -> tuple[list[RetrievedChunk], list[str]]:
    """多 KB 检索（2.8："多 KB 结果合并后重排"）；返回 `(命中, 实际使用的 kb_ids)`。

    **多 KB 必须同 embedding 模型**：不同模型的向量不在同一空间，分数没有可比性 ——
    这是"合并后重排"能成立的前提（附录 F v1.16 记录该约束）。
    """
    resolved = settings or get_settings()
    kbs = await get_knowledge_bases_by_ids(session, kb_ids)
    found = {kb.id for kb in kbs}
    missing = [kb_id for kb_id in kb_ids if kb_id not in found]
    if missing:
        raise KnowledgeBaseNotFoundError("Some knowledge bases were not found", details={"kb_ids": missing})
    if not kbs:
        return [], []
    models = {kb.embedding_model for kb in kbs}
    if len(models) > 1:
        raise ValidationError(
            "Retrieval across knowledge bases requires the same embedding model",
            details={"embedding_models": sorted(models)},
        )
    provider = await session.get(ModelProvider, kbs[0].embedding_provider_id)
    if provider is None:
        raise KnowledgeBaseIngestFailedError("Embedding provider disappeared", details={"stage": "embedding"})
    embedder = build_kb_embedder(provider, model=kbs[0].embedding_model, settings=resolved)
    store = build_vector_store(kind=kbs[0].vector_store_kind, chroma_dir=resolved.chroma_path)
    config = retrieval_config(kbs, top_k=top_k, score_threshold=score_threshold)
    chunks = await Retriever(embedder=embedder, store=store).retrieve(query, config)
    return chunks, list(config.kb_ids)


async def query_knowledge_base(
    session: AsyncSession,
    kb_id: str,
    payload: QueryRequest,
    *,
    settings: Settings | None = None,
) -> tuple[list[RetrievedChunk], list[str]]:
    """`POST /knowledge-bases/{id}/query`（3.2.5）。

    - 单 KB（未传 `kb_ids`）且集合为空 → `KB_EMPTY_INDEX`（409，附录 A：先上传文档）；
    - 多 KB 试算（传了 `kb_ids`）不做空检查 —— 返回空列表就是"没命中"，那是正常结果（4.6.3）。
    """
    resolved = settings or get_settings()
    kb = await get_knowledge_base(session, kb_id)
    if not payload.kb_ids:
        store = build_vector_store(kind=kb.vector_store_kind, chroma_dir=resolved.chroma_path)
        if await store.count(kb.collection_name) == 0:
            raise KnowledgeBaseEmptyIndexError(
                f"Knowledge base '{kb.name}' has no indexed chunks yet",
                details={"kb_id": kb.id, "collection": kb.collection_name},
            )
        kb_ids: list[str] = [kb_id]
    else:
        kb_ids = list(payload.kb_ids)
    return await retrieve(
        session,
        kb_ids,
        payload.query,
        top_k=payload.top_k,
        score_threshold=payload.score_threshold,
        settings=resolved,
    )
