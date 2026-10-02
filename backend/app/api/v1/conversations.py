"""会话路由（详细设计 3.2.4）。

`POST /conversations/{id}/messages`（SSE）在 `chat.py`，两者共用 `/conversations` 前缀。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, idempotency_key
from app.core.errors import ValidationError
from app.schemas.common import ApiResponse
from app.schemas.conversation import (
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from app.services import conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])

CREATE_SCOPE = "conversations.create"


@router.get("", response_model=ApiResponse[list[ConversationRead]], summary="会话列表（?agent_id=&q=）")
async def list_conversations(
    session: SessionDep,
    agent_id: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[list[ConversationRead]]:
    rows, total = await conversation_service.list_conversations(
        session, agent_id=agent_id, q=q, page=page, page_size=page_size
    )
    return ApiResponse[list[ConversationRead]].of(
        [ConversationRead.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "",
    response_model=ApiResponse[ConversationRead],
    status_code=status.HTTP_201_CREATED,
    summary="创建会话（{agent_id, title?}）",
)
async def create_conversation(
    payload: ConversationCreate,
    request: Request,
    session: SessionDep,
    store: IdempotencyDep,
) -> ApiResponse[ConversationRead] | JSONResponse:
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)

    conversation = await conversation_service.create_conversation(session, payload)
    response = ApiResponse[ConversationRead].of(ConversationRead.model_validate(conversation))
    if key:
        store.put(CREATE_SCOPE, key, status_code=status.HTTP_201_CREATED, payload=response.model_dump(mode="json"))
    return response


@router.get("/{conversation_id}", response_model=ApiResponse[ConversationRead], summary="会话详情")
async def get_conversation(
    conversation_id: str,
    session: SessionDep,
) -> ApiResponse[ConversationRead]:
    conversation = await conversation_service.get_conversation(session, conversation_id)
    return ApiResponse[ConversationRead].of(ConversationRead.model_validate(conversation))


@router.patch("/{conversation_id}", response_model=ApiResponse[ConversationRead], summary="重命名 / 归档")
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    session: SessionDep,
) -> ApiResponse[ConversationRead]:
    conversation = await conversation_service.update_conversation(session, conversation_id, payload)
    return ApiResponse[ConversationRead].of(ConversationRead.model_validate(conversation))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除会话")
async def delete_conversation(conversation_id: str, session: SessionDep) -> None:
    await conversation_service.delete_conversation(session, conversation_id)


@router.get(
    "/{conversation_id}/messages",
    response_model=ApiResponse[list[MessageRead]],
    summary="消息列表（游标分页，默认最近 50 条）",
)
async def list_messages(
    conversation_id: str,
    session: SessionDep,
    limit: int = Query(default=conversation_service.MESSAGE_PAGE_LIMIT, ge=1, le=conversation_service.MESSAGE_PAGE_MAX),
    cursor: str | None = Query(default=None, description="上一页返回的 meta.next_cursor"),
) -> ApiResponse[list[MessageRead]]:
    rows, next_cursor = await conversation_service.list_messages(
        session, conversation_id, limit=limit, cursor=parse_message_cursor(cursor)
    )
    return ApiResponse[list[MessageRead]].of(
        [MessageRead.model_validate(row) for row in rows],
        total=len(rows),
        next_cursor=str(next_cursor) if next_cursor is not None else None,
    )


def parse_message_cursor(cursor: str | None) -> int | None:
    """消息游标 = `messages.seq`；非法值 → `VALIDATION_ERROR`（422）。"""
    if cursor is None or cursor == "":
        return None
    try:
        return int(cursor)
    except ValueError as exc:
        raise ValidationError("`cursor` must be an integer `seq`", details={"cursor": cursor}) from exc
