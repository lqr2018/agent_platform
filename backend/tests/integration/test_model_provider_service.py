"""服务层直测：模型 Provider（详细设计 2.3 / 3.2.2 / 1.4 规则 1）。

覆盖 `api_key` 加解密与掩码、`is_default` 唯一化、被引用保护、连通性检测与默认 Provider 解析。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, ModelProviderNotFoundError, ValidationError
from app.schemas.llm import ModelEntry, ProviderCreate, ProviderUpdate
from app.services import model_provider_service
from tests.helpers import create_fake_provider

SECRET = "sk-abcdef123456"


def _payload(**overrides: object) -> ProviderCreate:
    data: dict[str, object] = {
        "name": "svc-provider-http",
        "base_url": "https://api.example.com",
        "api_key": SECRET,
        "default_model": "example-model",
        "models": [ModelEntry(name="example-model", input_price_per_1k_usd=0.001)],
    }
    data.update(overrides)
    return ProviderCreate(**data)


@pytest.mark.asyncio
async def test_create_encrypts_key_and_masks_response(session: AsyncSession) -> None:
    settings = get_settings()
    created = await model_provider_service.create_provider(session, _payload(), settings=settings)

    assert created.api_key_encrypted and SECRET not in created.api_key_encrypted
    read = model_provider_service.to_read(created, settings=settings)
    assert read.api_key_masked == "sk-***456" and read.has_api_key is True

    config = model_provider_service.to_config(created, settings=settings)
    assert config.api_key == SECRET  # 仅在内存中解密
    assert config.models[0]["name"] == "example-model"


@pytest.mark.asyncio
async def test_create_without_key_uses_platform_default(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_DEFAULT_API_KEY", "sk-platform-default")
    get_settings.cache_clear()
    settings = get_settings()

    created = await model_provider_service.create_provider(session, _payload(api_key=None), settings=settings)
    assert model_provider_service.to_config(created, settings=settings).api_key == "sk-platform-default"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_fake_kind_is_rejected_outside_test_env(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.app_env == "test"
    fake = await create_fake_provider(session, name="svc-fake-provider")
    assert fake.kind == "fake"

    monkeypatch.setenv("APP_ENV", "dev")
    get_settings.cache_clear()
    fake_payload = ProviderCreate.model_construct(**{**_payload(name="dev-fake").__dict__, "kind": "fake"})
    with pytest.raises(ValidationError):
        await model_provider_service.create_provider(session, fake_payload, settings=get_settings())
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_is_default_is_exclusive(session: AsyncSession) -> None:
    settings = get_settings()
    first = await model_provider_service.create_provider(session, _payload(is_default=True), settings=settings)
    second = await model_provider_service.create_provider(
        session, _payload(name="svc-provider-2", is_default=True), settings=settings
    )

    refreshed = await model_provider_service.get_provider(session, first.id)
    assert refreshed.is_default is False
    assert second.is_default is True

    default = await model_provider_service.ensure_default_provider(session)
    assert default is not None and default.id == second.id


@pytest.mark.asyncio
async def test_ensure_default_falls_back_to_earliest_enabled(session: AsyncSession) -> None:
    settings = get_settings()
    created = await model_provider_service.create_provider(session, _payload(), settings=settings)
    default = await model_provider_service.ensure_default_provider(session)
    assert default is not None and default.id == created.id


@pytest.mark.asyncio
async def test_list_filters_and_unknown_id(session: AsyncSession) -> None:
    settings = get_settings()
    await model_provider_service.create_provider(session, _payload(), settings=settings)

    assert len(await model_provider_service.list_providers(session, q="svc-provider")) == 1
    assert await model_provider_service.list_providers(session, q="missing") == []
    assert len(await model_provider_service.list_providers(session, status="enabled")) == 1
    assert await model_provider_service.list_providers(session, status="disabled") == []

    with pytest.raises(ModelProviderNotFoundError):
        await model_provider_service.get_provider(session, "01J8Z0000000000000000000ZZ")


@pytest.mark.asyncio
async def test_update_key_and_name_conflict(session: AsyncSession) -> None:
    settings = get_settings()
    first = await model_provider_service.create_provider(session, _payload(), settings=settings)
    await model_provider_service.create_provider(session, _payload(name="svc-provider-3"), settings=settings)

    updated = await model_provider_service.update_provider(
        session,
        first.id,
        ProviderUpdate.model_validate({"api_key": "sk-rotated999", "default_model": "m2"}),
        settings=settings,
    )
    assert model_provider_service.to_config(updated, settings=settings).api_key == "sk-rotated999"
    assert model_provider_service.to_read(updated, settings=settings).api_key_masked == "sk-***999"
    assert updated.default_model == "m2"

    cleared = await model_provider_service.update_provider(
        session, first.id, ProviderUpdate.model_validate({"api_key": ""}), settings=settings
    )
    assert cleared.api_key_encrypted is None
    assert model_provider_service.to_read(cleared, settings=settings).api_key_masked == ""

    with pytest.raises(ConflictError):
        await model_provider_service.update_provider(
            session, first.id, ProviderUpdate.model_validate({"name": "svc-provider-3"}), settings=settings
        )
    with pytest.raises(ConflictError):
        await model_provider_service.create_provider(session, _payload(name="svc-provider-3"), settings=settings)


@pytest.mark.asyncio
async def test_check_provider_ok_and_missing_model(session: AsyncSession) -> None:
    settings = get_settings()
    fake = await create_fake_provider(session, name="svc-check-provider")
    result = await model_provider_service.check_provider(session, fake.id, settings=settings)
    assert result.ok is True and result.model == "fake-model"
    assert (await model_provider_service.get_provider(session, fake.id)).last_check_at is not None

    without_model = await model_provider_service.create_provider(
        session, _payload(name="no-model", default_model=None, models=[]), settings=settings
    )
    with pytest.raises(ValidationError) as excinfo:
        await model_provider_service.check_provider(session, without_model.id, settings=settings)
    assert excinfo.value.details["provider_id"] == without_model.id


@pytest.mark.asyncio
async def test_delete_provider(session: AsyncSession) -> None:
    settings = get_settings()
    provider = await model_provider_service.create_provider(session, _payload(), settings=settings)
    await model_provider_service.delete_provider(session, provider.id)
    with pytest.raises(ModelProviderNotFoundError):
        await model_provider_service.get_provider(session, provider.id)
