"""模型 Provider 的 CRUD 与连通性检测（详细设计 3.2.2 / 2.3 / 1.4 规则 1）。

- `api_key` 只以 Fernet 密文落库，响应只给 `api_key_masked`；
- 行 → `ProviderConfig` 快照的装配在这里完成（1.2：runtime 不 import ORM）；
- `kind=fake` 只在 `APP_ENV=test` 时允许（4.1.3 的 Phase 1 约定），供集成测试造测试替身。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import SecretCipher, mask_secret
from app.core.errors import AppError, ConflictError, ModelProviderNotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import Agent, ModelProvider
from app.db.models.llm import PROVIDER_STATUS_ENABLED
from app.runtime.llm.base import ChatMessage, ProviderConfig
from app.runtime.llm.registry import LLMRegistry
from app.schemas.llm import ProviderCreate, ProviderRead, ProviderTestResult, ProviderUpdate

logger = get_logger(__name__)

TEST_ONLY_KINDS = frozenset({"fake"})
"""只在测试环境可写的 kind（4.1.3；`ProviderKind` 里没有它们，API 层会被 pydantic 拦下）。"""


def to_config(provider: ModelProvider, *, settings: Settings) -> ProviderConfig:
    """ORM 行 → runtime 快照（解密 `api_key`，仅存在于内存）。"""
    api_key = SecretCipher(settings.encryption_key).decrypt(provider.api_key_encrypted)
    return ProviderConfig(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key=api_key or settings.llm_default_api_key,
        default_model=provider.default_model,
        models=tuple(provider.models or ()),
        default_params=dict(provider.default_params or {}),
        headers={str(key): str(value) for key, value in (provider.headers or {}).items()},
    )


def to_read(provider: ModelProvider, *, settings: Settings) -> ProviderRead:
    """ORM 行 → 响应 DTO（**只含掩码**，1.4 规则 1）。"""
    plain = SecretCipher(settings.encryption_key).decrypt(provider.api_key_encrypted)
    return ProviderRead(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_masked=mask_secret(plain),
        has_api_key=bool(plain or settings.llm_default_api_key),
        default_model=provider.default_model,
        models=list(provider.models or []),
        default_params=dict(provider.default_params or {}),
        headers=dict(provider.headers or {}),
        is_default=provider.is_default,
        status=provider.status,
        last_check_at=provider.last_check_at,
        last_check_error=provider.last_check_error,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def list_providers(
    session: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> Sequence[ModelProvider]:
    statement = select(ModelProvider).order_by(ModelProvider.created_at.desc()).limit(limit)
    if q:
        statement = statement.where(ModelProvider.name.contains(q))
    if status:
        statement = statement.where(ModelProvider.status == status)
    return (await session.execute(statement)).scalars().all()


async def get_provider(session: AsyncSession, provider_id: str) -> ModelProvider:
    """取 Provider；不存在 → `MODEL_PROVIDER_NOT_FOUND`（404）。"""
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise ModelProviderNotFoundError(f"Model provider '{provider_id}' does not exist")
    return provider


async def create_provider(session: AsyncSession, data: ProviderCreate, *, settings: Settings) -> ModelProvider:
    """新建 Provider（含 `kind` 校验、`is_default` 唯一化、`api_key` 加密）。"""
    _validate_kind(data.kind, settings=settings)
    exists = (await session.execute(select(ModelProvider.id).where(ModelProvider.name == data.name))).first()
    if exists:
        raise ConflictError(f"Model provider name '{data.name}' already exists", details={"name": data.name})

    api_key = data.api_key if data.api_key is not None else settings.llm_default_api_key
    provider = ModelProvider(
        name=data.name,
        kind=str(data.kind),
        base_url=data.base_url,
        api_key_encrypted=SecretCipher(settings.encryption_key).encrypt(api_key),
        default_model=data.default_model,
        models=[entry.model_dump(exclude_none=True) for entry in data.models],
        default_params=dict(data.default_params),
        headers=dict(data.headers),
        is_default=data.is_default,
        status=data.status,
    )
    session.add(provider)
    await session.flush()
    if data.is_default:
        await _clear_other_defaults(session, keep_id=provider.id)
    await session.commit()
    await session.refresh(provider)
    return provider


async def update_provider(
    session: AsyncSession,
    provider_id: str,
    data: ProviderUpdate,
    *,
    settings: Settings,
) -> ModelProvider:
    """`PATCH` 语义：只改显式给出的字段（`api_key` 传空串 = 清除）。"""
    provider = await get_provider(session, provider_id)
    changes = data.model_dump(exclude_unset=True)

    name = changes.get("name")
    if name is not None and name != provider.name:
        exists = (await session.execute(select(ModelProvider.id).where(ModelProvider.name == name))).first()
        if exists:
            raise ConflictError(f"Model provider name '{name}' already exists", details={"name": name})

    if "api_key" in changes:
        raw_key = changes.pop("api_key")
        api_key = settings.llm_default_api_key if raw_key is None else raw_key
        provider.api_key_encrypted = SecretCipher(settings.encryption_key).encrypt(api_key)

    for field, value in changes.items():
        setattr(provider, field, value)

    await session.flush()
    if changes.get("is_default"):
        await _clear_other_defaults(session, keep_id=provider.id)
    await session.commit()
    await session.refresh(provider)
    return provider


async def delete_provider(session: AsyncSession, provider_id: str) -> None:
    """删除；被 Agent 引用 → `CONFLICT`（3.2.2）。"""
    provider = await get_provider(session, provider_id)
    statement = select(Agent.id).where(Agent.model_provider_id == provider_id).limit(1)
    referenced = (await session.execute(statement)).first()
    if referenced:
        raise ConflictError(
            "Model provider is referenced by an agent and cannot be deleted",
            details={"provider_id": provider_id, "agent_id": referenced[0]},
        )
    await session.delete(provider)
    await session.commit()


async def check_provider(
    session: AsyncSession,
    provider_id: str,
    *,
    settings: Settings,
    registry: LLMRegistry | None = None,
) -> ProviderTestResult:
    """连通性检测（`POST /model-providers/{id}/test`，3.2.2）：一次最小对话（`max_tokens=1`）。"""
    provider = await get_provider(session, provider_id)
    model = provider.default_model or next(iter(_model_names(provider)), "")
    if not model:
        raise ValidationError(
            "Provider has no model configured; set default_model or add a model to the whitelist",
            details={"provider_id": provider_id},
        )

    started = perf_counter()
    ok = True
    error_code: str | None = None
    error_message: str | None = None
    try:
        llm = (registry or LLMRegistry(settings)).create(to_config(provider, settings=settings))
        await llm.chat([ChatMessage.user("ping")], model=model, params={"max_tokens": 1}, stream=False)
    except AppError as exc:
        ok = False
        error_code = str(exc.code)
        error_message = exc.message
    latency_ms = int((perf_counter() - started) * 1000)

    provider.last_check_at = _utcnow()
    provider.last_check_error = None if ok else f"{error_code}: {error_message}"
    await session.commit()
    logger.info(
        "provider.tested",
        provider_id=provider_id,
        model=model,
        ok=ok,
        latency_ms=latency_ms,
        error_code=error_code,
    )
    return ProviderTestResult(
        ok=ok,
        provider_id=provider_id,
        model=model,
        latency_ms=latency_ms,
        error_code=error_code,
        error_message=error_message,
    )


async def ensure_default_provider(session: AsyncSession) -> ModelProvider | None:
    """取平台默认 Provider（`is_default=true`，没有则取最早的 enabled 行）。"""
    default = (
        await session.execute(select(ModelProvider).where(ModelProvider.is_default.is_(True)).limit(1))
    ).scalar_one_or_none()
    if default is not None:
        return default
    return (
        await session.execute(
            select(ModelProvider)
            .where(ModelProvider.status == PROVIDER_STATUS_ENABLED)
            .order_by(ModelProvider.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


def _validate_kind(kind: object, *, settings: Settings) -> None:
    """`fake` 仅测试环境可写；其余取值由 `ProviderKind` 约束（0.2.2 的 A 段）。"""
    if str(kind) in TEST_ONLY_KINDS and settings.app_env != "test":
        raise ValidationError(
            f"Provider kind '{kind}' is only available when APP_ENV=test",
            details={"kind": str(kind), "app_env": settings.app_env},
        )


async def _clear_other_defaults(session: AsyncSession, *, keep_id: str) -> None:
    """`is_default` 全平台唯一（2.3）。"""
    await session.execute(update(ModelProvider).where(ModelProvider.id != keep_id).values(is_default=False))


def _model_names(provider: ModelProvider) -> tuple[str, ...]:
    return tuple(str(item.get("name")) for item in (provider.models or []) if item.get("name"))


def _utcnow() -> datetime:
    """`last_check_at` 用 naive UTC（与 `db/base.now_utc` 一致，0.2.1）。"""
    return datetime.now(UTC).replace(tzinfo=None)
