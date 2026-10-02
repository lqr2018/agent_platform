"""Chat SSE 端点（详细设计 3.4 / 7.2 任务 6）。

- `POST /conversations/{id}/messages` → `text/event-stream`；
- 每 15s 发一次 `heartbeat`，最后一定发 `done`（3.4）；
- `DETACH_CANCEL=false`（默认）：客户端断开**不取消** Run，刷新页面仍能看到历史（3.4）；
  `DETACH_CANCEL=true` 时才随断连取消。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import SessionDep, SettingsDep
from app.core.config import Settings
from app.core.events import (
    SSE_HEADERS,
    DonePayload,
    HeartbeatPayload,
    SseEventType,
    format_sse,
)
from app.core.logging import get_logger
from app.schemas.conversation import MessageCreate
from app.services import chat_service

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["chat"])

HEARTBEAT_SECONDS = 15.0
"""3.4：每 15s 一次心跳，防代理断连。"""


@router.post(
    "/{conversation_id}/messages",
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流（3.4）"}},
    summary="一轮对话，返回 SSE 流",
)
async def create_message(
    conversation_id: str,
    payload: MessageCreate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> StreamingResponse:
    chat_run = await chat_service.start_chat(
        session, settings, conversation_id=conversation_id, content=payload.content
    )
    return StreamingResponse(
        _stream(chat_run, settings),
        media_type="text/event-stream",
        headers=dict(SSE_HEADERS),
    )


async def _stream(chat_run: chat_service.ChatRun, settings: Settings) -> AsyncIterator[str]:
    """队列 → SSE 帧（含心跳与 `done` 哨兵）。"""
    try:
        while True:
            try:
                item = await asyncio.wait_for(chat_run.queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield format_sse(SseEventType.HEARTBEAT, HeartbeatPayload(ts=datetime.now(UTC)))
                continue
            if item is None:
                break
            event, data = item
            yield format_sse(event, data)
        yield format_sse(SseEventType.DONE, DonePayload())
    finally:
        # 3.4：默认不取消（刷新页面仍可续看历史）；显式打开 DETACH_CANCEL 才随断连取消
        if settings.detach_cancel and not chat_run.task.done():
            logger.info("chat.detach_cancel", run_id=chat_run.run_id)
            chat_run.cancel_event.set()
