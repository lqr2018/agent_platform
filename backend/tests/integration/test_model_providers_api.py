"""模型 Provider API（详细设计 3.2.2 / 1.4 规则 1 / 1.5.5）。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import get_idempotency_store
from tests.helpers import create_agent, create_fake_provider

PROVIDER_BODY = {
    "name": "deepseek",
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-abcdef123456",
    "default_model": "deepseek-chat",
    "models": [{"name": "deepseek-chat", "input_price_per_1k_usd": 0.0002}],
}


@pytest_asyncio.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """复用 conftest 的 `session` fixture（统一 `dispose_engine()` 收尾）。"""
    return session


@pytest.fixture(autouse=True)
def _clean_idempotency() -> None:
    get_idempotency_store().clear()


@pytest.mark.asyncio
async def test_create_provider_masks_api_key(app_client: AsyncClient) -> None:
    response = await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY)
    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data["api_key_masked"] == "sk-***456"
    assert data["has_api_key"] is True
    assert "api_key" not in data and "api_key_encrypted" not in data
    assert data["base_url"] == "https://api.deepseek.com"


@pytest.mark.asyncio
async def test_list_get_patch_and_delete(app_client: AsyncClient) -> None:
    created = (await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY)).json()["data"]
    provider_id = created["id"]

    listing = await app_client.get("/api/v1/model-providers", params={"q": "deep"})
    assert [row["id"] for row in listing.json()["data"]] == [provider_id]

    detail = await app_client.get(f"/api/v1/model-providers/{provider_id}")
    assert detail.json()["data"]["name"] == "deepseek"

    patched = await app_client.patch(
        f"/api/v1/model-providers/{provider_id}", json={"is_default": True, "status": "disabled"}
    )
    assert patched.json()["data"]["is_default"] is True
    assert patched.json()["data"]["status"] == "disabled"

    assert (await app_client.delete(f"/api/v1/model-providers/{provider_id}")).status_code == 204
    assert (await app_client.get(f"/api/v1/model-providers/{provider_id}")).status_code == 404


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(app_client: AsyncClient) -> None:
    await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY)
    again = await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


@pytest.mark.asyncio
async def test_delete_referenced_provider_conflicts(app_client: AsyncClient, db_session: AsyncSession) -> None:
    provider = await create_fake_provider(db_session, name="referenced-provider")
    await create_agent(app_client, provider_id=str(provider.id), name="ref-agent")

    response = await app_client.delete(f"/api/v1/model-providers/{provider.id}")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["provider_id"] == provider.id


@pytest.mark.asyncio
async def test_fake_kind_is_rejected_by_api(app_client: AsyncClient) -> None:
    """0.2.2 A 段：`ProviderKind` 里没有 `fake`，API 直接 422。"""
    body = {**PROVIDER_BODY, "name": "fake-via-api", "kind": "fake"}
    response = await app_client.post("/api/v1/model-providers", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_test_endpoint_reports_ok_for_fake_provider(app_client: AsyncClient, db_session: AsyncSession) -> None:
    provider = await create_fake_provider(db_session, name="testable-provider")
    response = await app_client.post(f"/api/v1/model-providers/{provider.id}/test")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True and data["model"] == "fake-model"

    detail = await app_client.get(f"/api/v1/model-providers/{provider.id}")
    assert detail.json()["data"]["last_check_at"] is not None
    assert detail.json()["data"]["last_check_error"] is None


@pytest.mark.asyncio
async def test_test_endpoint_reports_error(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """上游报错时 `/test` 仍返回 200，但 `ok=false` 且写回 `last_check_error`。"""
    provider = await create_fake_provider(db_session, name="failing-provider", default_model="fake-model")
    # 让 fake 一律抛 MODEL_TIMEOUT：把 `simulate_error` 放进 provider 的 default_params
    provider.default_params = {"simulate_error": "timeout"}
    await db_session.commit()

    response = await app_client.post(f"/api/v1/model-providers/{provider.id}/test")
    data = response.json()["data"]
    assert data["ok"] is False and data["error_code"] == "MODEL_TIMEOUT"

    detail = await app_client.get(f"/api/v1/model-providers/{provider.id}")
    assert "MODEL_TIMEOUT" in detail.json()["data"]["last_check_error"]


@pytest.mark.asyncio
async def test_idempotency_key_replays_first_response(app_client: AsyncClient) -> None:
    """1.5.5：同一 `Idempotency-Key` 重复提交回放首次结果，不重复建行。"""
    headers = {"Idempotency-Key": "create-provider-once"}
    first = await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY, headers=headers)
    second = await app_client.post("/api/v1/model-providers", json=PROVIDER_BODY, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    listing = await app_client.get("/api/v1/model-providers")
    assert len(listing.json()["data"]) == 1
