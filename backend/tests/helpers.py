"""测试公共helpers（详细设计 9.2：测试替身复用规则）。

- `create_fake_provider()`：直接经 service 造一条 `kind=fake` 的 Provider ——
  4.1.3 约定 fake 只在 `APP_ENV=test` 时可写，且**不经 HTTP**（`ProviderKind` 里没有它）；
- `create_agent()` / `create_conversation()`：集成测试的搭台步骤（走 API，保证契约被覆盖）。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ModelProvider
from app.schemas.llm import ModelEntry, ProviderCreate
from app.services import model_provider_service

FAKE_MODEL = "fake-model"
"""测试替身暴露的模型名（与 `models[]` 白名单一致）。"""


async def create_fake_provider(
    session: AsyncSession,
    *,
    name: str = "fake-provider",
    default_model: str = FAKE_MODEL,
) -> ModelProvider:
    """造一条 fake Provider（`model_construct` 绕过 `ProviderKind` 校验，理由见 4.1.3）。"""
    payload = ProviderCreate.model_construct(
        name=name,
        kind="fake",
        base_url="fake://",
        api_key=None,
        default_model=default_model,
        models=[ModelEntry(name=default_model, input_price_per_1k_usd=0.001, output_price_per_1k_usd=0.002)],
        default_params={},
        headers={},
        is_default=False,
        status="enabled",
    )
    return await model_provider_service.create_provider(session, payload, settings=get_settings())


async def create_agent(
    client: AsyncClient,
    *,
    provider_id: str,
    name: str = "test-agent",
    system_prompt: str = "你是测试助手。",
    model_name: str = FAKE_MODEL,
    **overrides: object,
) -> dict[str, object]:
    """经 API 建一个 Agent（返回值即 `data`，便于直接取 id）。"""
    body: dict[str, object] = {
        "name": name,
        "model_provider_id": provider_id,
        "model_name": model_name,
        "system_prompt": system_prompt,
    }
    body.update(overrides)
    response = await client.post("/api/v1/agents", json=body)
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()["data"]
    return data


async def create_conversation(client: AsyncClient, *, agent_id: str, title: str | None = None) -> str:
    payload: dict[str, object] = {"agent_id": agent_id}
    if title is not None:
        payload["title"] = title
    response = await client.post("/api/v1/conversations", json=payload)
    assert response.status_code == 201, response.text
    conversation_id: str = response.json()["data"]["id"]
    return conversation_id


async def collect_sse(response_text: str) -> list[tuple[str, dict[str, object]]]:
    """把 SSE 原始文本解析成 `[(event, payload)]`（与前端 `api/sse.ts` 的分帧规则一致）。"""
    import json

    events: list[tuple[str, dict[str, object]]] = []
    for block in response_text.split("\n\n"):
        event_name = ""
        data_line = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data_line = line[len("data: ") :].strip()
        if event_name:
            payload = json.loads(data_line) if data_line else {}
            events.append((event_name, payload))
    return events
