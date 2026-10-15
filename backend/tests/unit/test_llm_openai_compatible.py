"""OpenAI-compatible Provider（详细设计 4.1.2 的七条要点）。

用 `httpx.MockTransport` 模拟上游（不引入 `respx`，7.2 交付物已注明），
流式响应按 `data:` 分帧喂给 Provider，覆盖：流式解析、`tool_calls` 增量拼接、
usage 缺失时的估算、错误映射、参数白名单与错误映射；另有 DeepSeek 侧的协议差异：
思考模式的 `reasoning_content`（与 `content` 同级、带 tools 时要回传）、
`thinking` / `reasoning_effort` 透传、402 余额不足、网关的纯文本错误体、
以及"上游没有 `/embeddings`"的可执行诊断。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.core.errors import (
    ModelAuthFailedError,
    ModelBadRequestError,
    ModelContextOverflowError,
    ModelProviderUnavailableError,
    ModelRateLimitedError,
)
from app.runtime.llm.base import ChatMessage, ProviderConfig, ToolCallSpec
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
    # 官方文档给的是**完整端点路径**（DeepSeek 的 curl 示例就是 /chat/completions），
    # 整条粘进 base_url 时先剥掉端点再补 /v1，避免拼成 .../chat/completions/v1
    assert normalize_base_url("https://api.deepseek.com/chat/completions") == "https://api.deepseek.com/v1"
    assert normalize_base_url("https://api.deepseek.com/v1/chat/completions/") == "https://api.deepseek.com/v1"
    assert normalize_base_url("https://api.deepseek.com/embeddings") == "https://api.deepseek.com/v1"


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
        # DeepSeek 402 - Insufficient Balance：账户没钱，不是参数问题（也不该重试）
        (402, {"error": {"message": "Insufficient Balance", "type": "unknown_error"}}, ModelAuthFailedError),
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


class _UnreadBody(httpx.AsyncByteStream):
    """还原"错误体还没读"的时序：真实上游的流式响应不会像 MockTransport 那样预先读完 body。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload


@pytest.mark.asyncio
async def test_stream_error_body_is_read_before_mapping() -> None:
    """LLM_STREAM 默认 true 时错误走流式分支：必须先读出错误体，才能按附录 A 映射。"""
    body = json.dumps({"error": {"message": "invalid api key"}}).encode("utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"content-type": "application/json"}, stream=_UnreadBody(body))

    provider = _provider(handler, stream=True)
    with pytest.raises(ModelAuthFailedError) as excinfo:
        await provider.chat([ChatMessage.user("hi")], model="m")
    assert excinfo.value.details.get("upstream_status") == 401


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


@pytest.mark.asyncio
async def test_thinking_params_are_forwarded_when_given() -> None:
    """`thinking` / `reasoning_effort` 在透传白名单里（DeepSeek 思考模式开关），但只在显式给出时下发。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(handler)
    await provider.chat([ChatMessage.user("hi")], model="m")
    assert "thinking" not in seen["body"]  # 没给就不下发（对普通 Provider 零影响）

    await provider.chat(
        [ChatMessage.user("hi")],
        model="m",
        params={"thinking": {"type": "disabled"}, "reasoning_effort": "low"},
    )
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["body"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_non_stream_parses_reasoning_content() -> None:
    """思考模式的 CoT 在 `message.reasoning_content`（与 `content` 同级），不能丢。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "答案", "reasoning_content": "先算一下"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )

    result = await _provider(handler).chat([ChatMessage.user("问题")], model="m")

    assert result.message.content == "答案"
    assert result.message.reasoning_content == "先算一下"
    assert result.message.to_openai()["reasoning_content"] == "先算一下"


@pytest.mark.asyncio
async def test_reasoning_content_is_sent_back_with_tool_calls() -> None:
    """带 `tools` 的多轮对话必须把历史 CoT 回传（DeepSeek 官方要求），否则模型丢失推理链。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好了"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    assistant_turn = ChatMessage.assistant(
        "我查一下",
        (
            ToolCallSpec(
                id="call_1",
                name="calculator",
                arguments={"expression": "1+1"},
                raw_arguments='{"expression": "1+1"}',
            ),
        ),
        reasoning_content="用户要算 1+1，先调计算器",
    )
    messages = [ChatMessage.user("算 1+1"), assistant_turn, ChatMessage.tool("2", tool_call_id="call_1")]
    await _provider(handler).chat(messages, model="m", tools=[{"type": "function", "function": {"name": "calculator"}}])

    sent = seen["body"]["messages"]
    assert sent[1]["reasoning_content"] == "用户要算 1+1，先调计算器"
    assert sent[1]["tool_calls"][0]["function"]["name"] == "calculator"
    # 没有 CoT 的消息不带这个非标字段（否则会污染 OpenAI / vLLM 等实现）
    assert "reasoning_content" not in sent[0]
    assert "reasoning_content" not in sent[2]


@pytest.mark.asyncio
async def test_stream_accumulates_reasoning_content_without_emitting_deltas() -> None:
    """流式：CoT 分片累积进消息（供回传）但不进 `on_delta`；末块自带 usage（DeepSeek 无需 `stream_options`）。"""
    fragments = [
        {"choices": [{"delta": {"reasoning_content": "先"}}]},
        {"choices": [{"delta": {"reasoning_content": "想"}}]},
        {"choices": [{"delta": {"content": "答"}}]},
        {"choices": [{"delta": {"content": "案"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse(*fragments))

    deltas: list[str] = []

    async def on_delta(piece: str) -> None:
        deltas.append(piece)

    result = await _provider(handler, stream=True).chat([ChatMessage.user("hi")], model="m", on_delta=on_delta)

    assert deltas == ["答", "案"]  # 只有可见正文走 on_delta
    assert result.message.content == "答案"
    assert result.message.reasoning_content == "先想"
    assert result.usage.total_tokens == 5  # 直接采信末块的 usage，不猜
    assert result.usage_estimated is False


@pytest.mark.asyncio
async def test_embed_missing_endpoint_reports_actionable_hint() -> None:
    """上游没有 `/embeddings`（DeepSeek 即如此）时，报错要能直接照做，而不是笼统地让人去查模型名。"""
    provider = _provider(lambda _request: httpx.Response(404, json={"error": {"message": "Not Found"}}))

    with pytest.raises(ModelBadRequestError) as excinfo:
        await provider.embed(["a"], model="m")

    assert excinfo.value.details["upstream_status"] == 404
    assert "embeddings" in excinfo.value.message
    assert "seed_demo --fake-embedding" in str(excinfo.value.details["hint"])


@pytest.mark.asyncio
async def test_embed_other_bad_request_is_untouched() -> None:
    """其它 4xx（模型名写错等）保持原样，不塞"没有 /embeddings"的误导性 hint。"""
    provider = _provider(lambda _request: httpx.Response(400, json={"error": {"message": "invalid model"}}))

    with pytest.raises(ModelBadRequestError) as excinfo:
        await provider.embed(["a"], model="nope")

    assert "embeddings" not in excinfo.value.message
    assert "hint" not in excinfo.value.details


@pytest.mark.asyncio
async def test_plain_text_error_body_is_kept_in_details() -> None:
    """本机实测：DeepSeek 网关鉴权失败回的是纯文本（不是 JSON），原话要留进 details 才能排障。"""
    provider = _provider(lambda _request: httpx.Response(401, text="Authentication Fails (governor)"))

    with pytest.raises(ModelAuthFailedError) as excinfo:
        await provider.chat([ChatMessage.user("hi")], model="m")

    assert excinfo.value.details["upstream_message"] == "Authentication Fails (governor)"
