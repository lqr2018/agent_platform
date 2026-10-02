"""FastAPI 依赖（详细设计 1.1 / 1.5.5）。

- `get_app_settings`：配置单例（便于测试覆盖）；
- `get_session`：一个请求一个 DB session；
- `get_idempotency_store` + `idempotency_key`：写接口的 `Idempotency-Key` 支持（1.5.5）。

`get_current_scope`（SD-3：`owner_key` 固定 `local`）与 `get_runtime` 在 Phase 1 以
`services/chat_service.py` 的编排代替，不再单独提供依赖。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.idempotency import IdempotencyStore
from app.core.idempotency import get_idempotency_store as _get_store
from app.db.session import get_session

__all__ = [
    "IdempotencyDep",
    "SessionDep",
    "Settings",
    "SettingsDep",
    "get_app_settings",
    "get_idempotency_store",
    "get_session",
    "idempotency_key",
]

IDEMPOTENCY_HEADER = "Idempotency-Key"
"""1.5.5：写接口的幂等键头。"""


def get_app_settings() -> Settings:
    """配置单例依赖（便于测试覆盖）。"""
    return get_settings()


def get_idempotency_store() -> IdempotencyStore:
    """进程内幂等存储（1.5.5：不引入 Redis）。"""
    return _get_store()


def idempotency_key(request: Request) -> str | None:
    """取 `Idempotency-Key` 头（缺省或空串 → `None`，表示不做幂等回放）。"""
    value = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    return value or None


SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""请求级 DB session：用 `Annotated` 形式声明，避免 `B008`（函数调用作默认值）。"""

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
"""配置单例依赖。"""

IdempotencyDep = Annotated[IdempotencyStore, Depends(get_idempotency_store)]
"""幂等存储依赖（1.5.5）。"""
