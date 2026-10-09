"""知识库路由（详细设计 3.2.5 / 2.8 / 4.6）。

端点清单（3.2.5）：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/knowledge-bases` | 列表（含 `stats`） |
| POST | `/knowledge-bases` | 新建（探测 `embedding_dim`）→ 201 |
| GET / PATCH / DELETE | `/knowledge-bases/{id}` | 详情 / 更新 / 删除（含向量集合） |
| POST | `/knowledge-bases/{id}/query` | 直接检索（前端调试与评测用） |
| GET / POST | `/knowledge-bases/{id}/documents` | 文档列表 / 上传（multipart）→ 202 |
| GET / DELETE | `.../documents/{doc_id}` | 详情 / 删除（含向量） |
| POST | `.../documents/{doc_id}/reingest` | 重新摄取 → 202 |
| GET | `.../documents/{doc_id}/chunks` | 切片预览（分页） |

外加一个**运维只读端点** `GET /maintenance/knowledge-bases/{id}/verify-index`（7.6 的
"`verify_kb_index` 在 `/maintenance` 暴露"；用 GET 是因为它只读且幂等）。

`{source_type:"url"}` 形态（3.2.5 表格中的另一半）属**迭代 E**（4.6.1 的 `WebLoader`），
本阶段只接受 `multipart` 上传；传 url 会得到 `VALIDATION_ERROR`（422）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, SettingsDep, idempotency_key
from app.schemas.common import ApiResponse
from app.schemas.knowledge import (
    ChunkRead,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    MaintenanceResultRead,
    QueryHitRead,
    QueryRequest,
    QueryResultRead,
)
from app.services import kb_service

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
maintenance_router = APIRouter(prefix="/maintenance", tags=["maintenance"])

CREATE_SCOPE = "knowledge-bases.create"


@router.get("", response_model=ApiResponse[list[KnowledgeBaseRead]], summary="知识库列表（?q=）")
async def list_knowledge_bases(
    session: SessionDep,
    q: str | None = None,
) -> ApiResponse[list[KnowledgeBaseRead]]:
    rows = await kb_service.list_knowledge_bases(session, q=q)
    return ApiResponse[list[KnowledgeBaseRead]].of([KnowledgeBaseRead.model_validate(row) for row in rows])


@router.post(
    "",
    response_model=ApiResponse[KnowledgeBaseRead],
    status_code=status.HTTP_201_CREATED,
    summary="新建知识库（探测 embedding_dim）",
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    store: IdempotencyDep,
) -> ApiResponse[KnowledgeBaseRead] | JSONResponse:
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)
    kb = await kb_service.create_knowledge_base(session, payload, settings=settings)
    response = ApiResponse[KnowledgeBaseRead].of(KnowledgeBaseRead.model_validate(kb))
    if key:
        store.put(CREATE_SCOPE, key, status_code=status.HTTP_201_CREATED, payload=response.model_dump(mode="json"))
    return response


@router.get("/{kb_id}", response_model=ApiResponse[KnowledgeBaseRead], summary="知识库详情")
async def get_knowledge_base(kb_id: str, session: SessionDep) -> ApiResponse[KnowledgeBaseRead]:
    kb = await kb_service.get_knowledge_base(session, kb_id)
    return ApiResponse[KnowledgeBaseRead].of(KnowledgeBaseRead.model_validate(kb))


@router.patch("/{kb_id}", response_model=ApiResponse[KnowledgeBaseRead], summary="更新知识库")
async def update_knowledge_base(
    kb_id: str,
    payload: KnowledgeBaseUpdate,
    session: SessionDep,
) -> ApiResponse[KnowledgeBaseRead]:
    kb = await kb_service.update_knowledge_base(session, kb_id, payload)
    return ApiResponse[KnowledgeBaseRead].of(KnowledgeBaseRead.model_validate(kb))


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除知识库（含向量集合）")
async def delete_knowledge_base(kb_id: str, session: SessionDep, settings: SettingsDep) -> None:
    await kb_service.delete_knowledge_base(session, kb_id, settings=settings)


@router.post(
    "/{kb_id}/query",
    response_model=ApiResponse[QueryResultRead],
    summary="检索试算（?多 KB 用 kb_ids）",
)
async def query_knowledge_base(
    kb_id: str,
    payload: QueryRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[QueryResultRead]:
    chunks, kb_ids = await kb_service.query_knowledge_base(session, kb_id, payload, settings=settings)
    result = QueryResultRead(
        query=payload.query,
        kb_ids=kb_ids,
        hit_count=len(chunks),
        chunks=[
            QueryHitRead(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                kb_id=chunk.kb_id,
                score=round(chunk.score, 4),
                content=chunk.content,
                source=chunk.citation_source(),
                meta=dict(chunk.meta),
            )
            for chunk in chunks
        ],
    )
    return ApiResponse[QueryResultRead].of(result)


# ---- 文档级端点（3.2.5） ----


@router.get(
    "/{kb_id}/documents",
    response_model=ApiResponse[list[DocumentRead]],
    summary="文档列表（?status=）",
)
async def list_documents(
    kb_id: str,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> ApiResponse[list[DocumentRead]]:
    rows = await kb_service.list_documents(session, kb_id, status=status_filter)
    return ApiResponse[list[DocumentRead]].of([DocumentRead.model_validate(row) for row in rows])


@router.post(
    "/{kb_id}/documents",
    response_model=ApiResponse[DocumentRead],
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（multipart）→ 202 + pending",
)
async def upload_document(
    kb_id: str,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="md / txt 文档；pdf 与 url 属迭代 E")],
) -> ApiResponse[DocumentRead]:
    content = await file.read()
    document = await kb_service.create_document(
        session,
        kb_id,
        filename=file.filename or "upload.md",
        content=content,
        mime_type=file.content_type or "",
        settings=settings,
    )
    return ApiResponse[DocumentRead].of(DocumentRead.model_validate(document))


@router.get("/{kb_id}/documents/{doc_id}", response_model=ApiResponse[DocumentRead], summary="文档详情")
async def get_document(kb_id: str, doc_id: str, session: SessionDep) -> ApiResponse[DocumentRead]:
    document = await kb_service.get_document(session, kb_id, doc_id)
    return ApiResponse[DocumentRead].of(DocumentRead.model_validate(document))


@router.delete(
    "/{kb_id}/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档（含切片与向量）",
)
async def delete_document(kb_id: str, doc_id: str, session: SessionDep, settings: SettingsDep) -> None:
    await kb_service.delete_document(session, kb_id, doc_id, settings=settings)


@router.post(
    "/{kb_id}/documents/{doc_id}/reingest",
    response_model=ApiResponse[DocumentRead],
    status_code=status.HTTP_202_ACCEPTED,
    summary="重新摄取（改 chunk 参数后用）→ 202 + pending",
)
async def reingest_document(
    kb_id: str,
    doc_id: str,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[DocumentRead]:
    document = await kb_service.reingest_document(session, kb_id, doc_id, settings=settings)
    return ApiResponse[DocumentRead].of(DocumentRead.model_validate(document))


@router.get(
    "/{kb_id}/documents/{doc_id}/chunks",
    response_model=ApiResponse[list[ChunkRead]],
    summary="切片预览（分页）",
)
async def list_chunks(
    kb_id: str,
    doc_id: str,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=kb_service.MAX_CHUNK_PAGE_SIZE)] = 50,
) -> ApiResponse[list[ChunkRead]]:
    rows = await kb_service.list_chunks(session, kb_id, doc_id, offset=offset, limit=limit)
    return ApiResponse[list[ChunkRead]].of([ChunkRead.model_validate(row) for row in rows])


@maintenance_router.get(
    "/knowledge-bases/{kb_id}/verify-index",
    response_model=ApiResponse[MaintenanceResultRead],
    summary="向量与 DB 的一致性对账",
)
async def verify_kb_index(
    kb_id: str,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[MaintenanceResultRead]:
    data = await kb_service.verify_kb_index(session, kb_id, settings=settings)
    ok = bool(data.get("ok"))
    detail = "chunks and vector items match" if ok else "mismatch: reingest the document or inspect the collection"
    return ApiResponse[MaintenanceResultRead].of(MaintenanceResultRead(ok=ok, detail=detail, data=data))
