"""模型 Provider 路由（详细设计 3.2.2 / 3.3.1）。

**响应永不含明文密钥**（1.4 规则 1）；写接口支持 `Idempotency-Key`（1.5.5）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import IdempotencyDep, SessionDep, SettingsDep, idempotency_key
from app.schemas.common import ApiResponse
from app.schemas.llm import ProviderCreate, ProviderRead, ProviderTestResult, ProviderUpdate
from app.services import model_provider_service

router = APIRouter(prefix="/model-providers", tags=["models"])

CREATE_SCOPE = "model_providers.create"
"""幂等键的命名空间（1.5.5）。"""


@router.get("", response_model=ApiResponse[list[ProviderRead]], summary="Provider 列表")
async def list_model_providers(
    session: SessionDep,
    settings: SettingsDep,
    q: str | None = None,
    status_filter: str | None = None,
) -> ApiResponse[list[ProviderRead]]:
    rows = await model_provider_service.list_providers(session, q=q, status=status_filter)
    return ApiResponse[list[ProviderRead]].of([model_provider_service.to_read(row, settings=settings) for row in rows])


@router.post(
    "",
    response_model=ApiResponse[ProviderRead],
    status_code=status.HTTP_201_CREATED,
    summary="新建 Provider（api_key 加密存储）",
)
async def create_model_provider(
    payload: ProviderCreate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    store: IdempotencyDep,
) -> ApiResponse[ProviderRead] | JSONResponse:
    key = idempotency_key(request)
    if key:
        cached = store.get(CREATE_SCOPE, key)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.payload)

    provider = await model_provider_service.create_provider(session, payload, settings=settings)
    response = ApiResponse[ProviderRead].of(model_provider_service.to_read(provider, settings=settings))
    if key:
        store.put(CREATE_SCOPE, key, status_code=status.HTTP_201_CREATED, payload=response.model_dump(mode="json"))
    return response


@router.get("/{provider_id}", response_model=ApiResponse[ProviderRead], summary="Provider 详情")
async def get_model_provider(
    provider_id: str,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[ProviderRead]:
    provider = await model_provider_service.get_provider(session, provider_id)
    return ApiResponse[ProviderRead].of(model_provider_service.to_read(provider, settings=settings))


@router.patch("/{provider_id}", response_model=ApiResponse[ProviderRead], summary="更新 Provider")
async def update_model_provider(
    provider_id: str,
    payload: ProviderUpdate,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[ProviderRead]:
    provider = await model_provider_service.update_provider(session, provider_id, payload, settings=settings)
    return ApiResponse[ProviderRead].of(model_provider_service.to_read(provider, settings=settings))


@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除 Provider（被引用 → 409）",
)
async def delete_model_provider(
    provider_id: str,
    session: SessionDep,
) -> None:
    await model_provider_service.delete_provider(session, provider_id)


@router.post(
    "/{provider_id}/test",
    response_model=ApiResponse[ProviderTestResult],
    summary="连通性检测（1-token）",
)
async def test_model_provider(
    provider_id: str,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiResponse[ProviderTestResult]:
    result = await model_provider_service.check_provider(session, provider_id, settings=settings)
    return ApiResponse[ProviderTestResult].of(result)
