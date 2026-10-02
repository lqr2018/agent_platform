"""OpenAI-compatible Provider（详细设计 4.1.2 的七条要点）。

用 `httpx.MockTransport` 模拟上游（不引入 `respx`，7.2 交付物已注明），
流式响应按 `data:` 分帧喂给 Provider，覆盖：流式解析、`tool_calls` 增量拼接、
usage 缺失时的估算、错误映射、参数白名单与错误映射。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.core.errors import (
    ModelAuthFailedError,
    ModelBadRequestError,
    ModelContextOverflowError,
    ModelProviderUnavailableError,
    ModelRateLimitedError,
)
from app.runtime.llm.base import ChatMessage, ProviderConfig
from app.runtime.llm.openai_compatible import OpenAICompatibleProvider, normalize_base_url

RequestHandler = Callable[[httpx.Request], httpx.Response]


def _provider(
    handler: RequestHandler,
    *,
    base_url: str = "https://api.example.com/v1",
    max_retries: int = 0,
    stream: bool = False,
    models: tuple[dict[str, object], ...] = (),
) -> OpenAICompatibleProvider:
    config = ProviderConfig(
        id="p1",
        name="p1",
        kind="openai_compatible",
        base_url=base_url,
        api_key="sk-test-key",
        models=models,
    )
    return OpenAICompatibleProvider(
        config,
        timeout_seconds=5.0,
        max_retries=max_retries,
        stream=stream,
        transport=httpx.MockTransport(handler),
    )


def _sse(*chunks: dict[str, object]) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return f"{body}data: [DONE]\n\n"


def test_normalize_base_url() -> None:
    assert normalize_base_url("https://api.openai.com") == "https://api.openai.com/v1"
    assert normalize_base_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"
    assert normalize_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_non_stream_chat_parses_message_usage_and_cost() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    prices = ({"name": "m", "input_price_per_1k_usd": 0.001, "output_price_per_1k_usd": 0.002},)
    provider = _provider(handler, models=prices)
    result = await provider.chat([ChatMessage.user("hi")], model="m", params={"temperature": 0.5, "top_k": 3})

    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-key"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "m" and body["stream"] is False
    assert body["temperature"] == 0.5
    assert "top_k" not in body  # 4.1.2 第 5 条：白名单外的参数不透传
    assert "tools" not in body  # 4.1.2 第 4 条：无工具时不下发

    assert result.message.content == "hello"
    assert result.usage.total_tokens == 15
    assert result.usage_estimated is False
    assert result.cost_estimated is True
    assert float(result.cost_usd) == pytest.approx(0.000018)


@pytest.mark.asyncio
async def test_stream_chat_merges_tool_call_fragments() -> None:
    fragments = [
        {"choices": [{"delta": {"content": "我"}}]},
        {"choices": [{"delta": {"content": "先算"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "calculator", "arguments": '{"expr'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ession": "1+1"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse(*fragments))

    provider = _provider(handler, stream=True)
    deltas: list[str] = []

    async def on_delta(piece: str) -> None:
        deltas.append(piece)

    result = await provider.chat([ChatMessage.user("算一下")], model="m", on_delta=on_delta)

    assert deltas == ["我", "先算"]
    assert result.message.content == "我先算"
    assert result.finish_reason == "tool_calls"
    assert len(result.message.tool_calls) == 1
    call = result.message.tool_calls[0]
    assert call.id == "call_1" and call.name == "calculator"
    assert call.arguments == {"expression": "1+1"}
    assert result.usage_estimated is True  # 上游没回 usage → 本地估算（4.1.2 第 6 条）


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": "invalid api key"}}, ModelAuthFailedError),
        (403, {"error": {"message": "forbidden"}}, ModelAuthFailedError),
        (429, {"error": {"message": "rate limited"}}, ModelRateLimitedError),
        (500, {"error": {"message": "boom"}}, ModelProviderUnavailableError),
        (400, {"error": {"message": "model not found"}}, ModelBadRequestError),
        (
            400,
            {"error": {"message": "This model's maximum context length is 8192 tokens"}},
            ModelContextOverflowError,
        ),
    ],
)
async def test_error_mapping(status: int, body: dict, expected: type[Exception]) -> None:
    provider = _provider(lambda _request: httpx.Response(status, json=body))
    with pytest.raises(expected) as excinfo:
        await provider.chat([ChatMessage.user("hi")], model="m")
    assert getattr(excinfo.value, "details", {}).get("upstream_status") == status


@pytest.mark.asyncio
async def test_retry_on_rate_limit_uses_backoff() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(handler, max_retries=1)
    result = await provider.chat([ChatMessage.user("hi")], model="m")
    assert len(calls) == 2  # 1.5.3：429 按退避重试一次
    assert result.message.content == "ok"


@pytest.mark.asyncio
async def test_connection_error_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = _provider(handler)
    with pytest.raises(ModelProviderUnavailableError):
        await provider.chat([ChatMessage.user("hi")], model="m")


@pytest.mark.asyncio
async def test_embed_parses_vectors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})

    provider = _provider(handler)
    assert await provider.embed(["a", "b"], model="m") == [[0.1, 0.2], [0.3, 0.4]]
