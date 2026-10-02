"""事件发射器（详细设计 4.4.1 / 3.4）。

`AgentRuntime` 只认 `EventEmitter` 协议：HTTP/SSE 用服务层的队列实现（`chat_service`），
Workflow / 评测 / 脚本用 `NullEmitter` 或 `ListEmitter`（不依赖 Web 框架，1.2）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.events import SseEventType

EventPayload = BaseModel | Mapping[str, Any] | None


class EventEmitter(Protocol):
    """4.4.1：`emit` 的注入点（SSE 回调）。"""

    async def emit(self, event: SseEventType, payload: EventPayload = None) -> None: ...


class NullEmitter:
    """不产生任何事件（`scripts/evaluate.py` / Workflow 内部复用 Runtime 时用）。"""

    async def emit(self, event: SseEventType, payload: EventPayload = None) -> None:
        return None


class ListEmitter:
    """把事件收进列表（单元测试断言顺序与 payload）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: SseEventType, payload: EventPayload = None) -> None:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload or {})
        self.events.append((str(event), data))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payload_for(self, event: SseEventType) -> dict[str, Any]:
        return next((data for name, data in self.events if name == str(event)), {})
