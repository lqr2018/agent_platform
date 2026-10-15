"""OpenAI-compatible Provider（详细设计 4.1.2）。

实现要点（与 4.1.2 的七条逐条对应）：

1. 只用 `httpx.AsyncClient`，不自带 OpenAI SDK；
2. `base_url` 归一化（补 `/v1`、去尾斜杠，兼容 DashScope / DeepSeek / vLLM / Ollama）；
3. 流式按 `data: ` 分帧、处理 `[DONE]`，`delta.tool_calls` 按 `index` **增量拼接**；
4. `tools` 为空时不下发该字段；有工具时 `tool_choice="auto"`；
5. 参数透传白名单（见 `PARAM_WHITELIST`）；
6. token 优先取上游 `usage`，缺失则本地估算并标 `usage_estimated`；
7. 成本按 `models[]` 价格估算，缺价格 → 0 且 `cost_estimated=false`（4.8.3）；
8. 思考型模型（DeepSeek 的 thinking 模式）：CoT 走与 `content` **同级**的 `reasoning_content`，
   流式与非流式都解析出来；**带 `tools` 的多轮对话把历史 CoT 回传**（上游要求，见 `ChatMessage.to_openai()`），
   `thinking` / `reasoning_effort` 按白名单透传（只透传调用方显式给的值）；
9. 上游没有 `/embeddings`（DeepSeek 只有 Chat 接口）时给出**可照做**的诊断，而不是笼统的"检查模型名"。

异常一律翻译为 `MODEL_*`（1.6 / 附录 A）；重试交给 `core/retry.py`，且**首 token 之后不再重试**（1.5.3）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from decimal import Decimal
from time import perf_counter
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    ModelAuthFailedError,
    ModelBadRequestError,
    ModelContextOverflowError,
    ModelProviderUnavailableError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from app.core.logging import get_logger
from app.core.retry import call_with_retry
from app.runtime.llm.base import (
    ChatMessage,
    DeltaCallback,
    LLMResult,
    ProviderConfig,
    ToolCallCallback,
    ToolCallSpec,
    parse_arguments,
)
from app.runtime.llm.usage import TokenUsage, estimate_tokens
from app.runtime.observability.cost import estimate_cost, has_price, parse_prices
from app.runtime.observability.tracer import Span

logger = get_logger(__name__)

PARAM_WHITELIST = (
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    # DeepSeek 的思考模式开关与力度（OpenAI 格式：`thinking={"type": "enabled"}` + `reasoning_effort`）。
    # 只在调用方（Provider 的 `default_params` 或 Agent 参数）显式给了值时才下发，
    # 因此对不认识这两个字段的 Provider 零影响。
    "thinking",
    "reasoning_effort",
)
"""4.1.2 第 5 条允许透传的采样参数。"""

ENDPOINT_SUFFIXES = ("/chat/completions", "/embeddings")
"""会被本类自行拼接的端点路径：`base_url` 里带了就先剥掉，避免拼成 `.../chat/completions/v1`。

官方文档给的是完整路径（DeepSeek：`curl https://api.deepseek.com/chat/completions`），
把文档里的整条地址粘进 Provider 表单是常见误操作。
"""

EMBEDDINGS_MISSING_HINTS = ("not found", "unsupported", "no such", "unknown url", "method not allowed")
"""上游"没有这条路由"的常见说法（除 404/405 外，有些网关用 400 + 文本表达）。"""

EMBEDDINGS_MISSING_MESSAGE = "Model provider does not expose an OpenAI-compatible /embeddings endpoint"

EMBEDDINGS_HINT = (
    "该 Provider 不提供 embeddings（DeepSeek 只有 Chat 接口）：知识库请另配一个支持 embeddings 的 Provider，"
    "或在开发环境用 `python -m app.scripts.seed_demo --fake-embedding`（确定性向量，需 EMBED_ALLOW_FAKE=true）"
)
"""`embed()` 遇到"没有 /embeddings"时塞进 `details.hint` 的下一步动作。"""

RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (ModelRateLimitedError, ModelProviderUnavailableError)
"""1.5.3：只对 429 / 5xx / 连接错误重试。"""

CONTEXT_OVERFLOW_HINTS = ("context length", "context_length", "maximum context", "too many tokens", "context window")


def normalize_base_url(base_url: str) -> str:
    """补 `/v1` 并去掉尾斜杠（4.1.2 第 2 条）。

    先把调用方粘进来的**完整端点路径**剥掉（见 `ENDPOINT_SUFFIXES`），再补 `/v1`：
    `https://api.deepseek.com/chat/completions` → `https://api.deepseek.com/v1`。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return url
    lowered = url.lower()
    for suffix in ENDPOINT_SUFFIXES:
        if lowered.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    if not url:
        return url
    return url if url.lower().endswith("/v1") else f"{url}/v1"


class OpenAICompatibleProvider:
    """4.1.1 协议的 OpenAI 兼容实现。"""

    name = "openai_compatible"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        stream: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.base_url = normalize_base_url(config.base_url)
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._stream_default = stream
        self._transport = transport
        self._prices = parse_prices(config.models)

    @classmethod
    def from_settings(
        cls,
        config: ProviderConfig,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> OpenAICompatibleProvider:
        return cls(
            config,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            stream=settings.llm_stream,
            transport=transport,
        )

    # ---- 4.1.1 接口 ----
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_delta: DeltaCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        trace_span: Span | None = None,
    ) -> LLMResult:
        """4.1.1：`chat` 是唯一对话入口；`stream=True` 时逐段回调 `on_delta` 并返回完整结果。"""
        emitted = False

        def mark_emitted() -> None:
            nonlocal emitted
            emitted = True

        async def attempt() -> LLMResult:
            nonlocal emitted
            emitted = False
            if stream or self._stream_default:
                return await self._chat_stream(
                    messages,
                    model=model,
                    tools=tools,
                    params=params,
                    on_delta=on_delta,
                    on_tool_call=on_tool_call,
                    on_first_delta=mark_emitted,
                )
            return await self._chat_once(messages, model=model, tools=tools, params=params, on_tool_call=on_tool_call)

        return await call_with_retry(
            attempt,
            times=self._max_retries,
            base_delay=0.5,
            retry_on=RETRYABLE_ERRORS,
            # 1.5.3：首 token 之后重试会造成重复内容，故不再重试
            should_retry=lambda _exc, _attempt: not emitted,
            event="llm.chat",
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        """`POST /embeddings`（4.1.2 第 9 条：上游没这条路由时给出能照做的诊断）。"""
        try:
            data = await self._post("/embeddings", {"model": model, "input": texts})
        except ModelBadRequestError as exc:
            raise self._embeddings_error(exc) from exc
        return [list(item.get("embedding") or []) for item in (data.get("data") or [])]

    def _embeddings_error(self, exc: ModelBadRequestError) -> ModelBadRequestError:
        """把"上游没有 `/embeddings`"换成带 `hint` 的报错（DeepSeek 即属此类）；其余 4xx 原样返回。"""
        status = exc.details.get("upstream_status")
        upstream = str(exc.details.get("upstream_message") or "").lower()
        if status in (404, 405) or any(hint in upstream for hint in EMBEDDINGS_MISSING_HINTS):
            return ModelBadRequestError(
                EMBEDDINGS_MISSING_MESSAGE,
                details={**exc.details, "hint": EMBEDDINGS_HINT},
            )
        return exc

    # ---- 内部：非流式 / 流式 ----
    async def _chat_once(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        params: Mapping[str, Any] | None,
        on_tool_call: ToolCallCallback | None,
    ) -> LLMResult:
        payload = self._payload(messages, model=model, tools=tools, params=params, stream=False)
        started = perf_counter()
        data = await self._post("/chat/completions", payload)
        latency_ms = int((perf_counter() - started) * 1000)

        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        raw_message = choice.get("message") or {}
        content = raw_message.get("content") or ""
        # 4.1.2 第 8 条：思考型模型的 CoT 与 content 同级（`message.reasoning_content`）
        reasoning = str(raw_message.get("reasoning_content") or "")
        tool_calls = tuple(self._parse_tool_calls(raw_message.get("tool_calls") or []))
        for call in tool_calls:
            if on_tool_call is not None:
                await on_tool_call(call)
        usage, usage_estimated = self._resolve_usage(data.get("usage"), messages, content + reasoning)
        return self._result(
            model=model,
            message=ChatMessage.assistant(content, tool_calls, reasoning_content=reasoning),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=usage,
            usage_estimated=usage_estimated,
            latency_ms=latency_ms,
            raw=data,
        )

    async def _chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        params: Mapping[str, Any] | None,
        on_delta: DeltaCallback | None,
        on_tool_call: ToolCallCallback | None,
        on_first_delta: Callable[[], None],
    ) -> LLMResult:
        payload = self._payload(messages, model=model, tools=tools, params=params, stream=True)
        started = perf_counter()
        content = ""
        reasoning = ""
        finish_reason = "stop"
        usage: TokenUsage | None = None
        accumulated: dict[int, dict[str, str]] = {}
        first_delta_seen = False

        async with (
            self._client() as client,
            client.stream("POST", self._url("/chat/completions"), json=payload, headers=self._headers()) as response,
        ):
            if response.status_code >= 400:
                await self._raise_for_status(response)
            async for chunk in self._iter_chunks(response):
                if chunk.get("usage"):
                    usage = self._usage_from_payload(chunk["usage"])
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                # 4.1.2 第 8 条：CoT 分片累积进消息（供带 tools 的多轮回传），
                # **不**经过 `on_delta` —— 那是给客户端的"可见正文"通道。
                reasoning_piece = delta.get("reasoning_content")
                if reasoning_piece:
                    reasoning += str(reasoning_piece)
                piece = delta.get("content")
                if piece:
                    content += piece
                    if not first_delta_seen:
                        first_delta_seen = True
                        on_first_delta()
                    if on_delta is not None:
                        await on_delta(piece)
                self._accumulate_tool_calls(accumulated, delta.get("tool_calls") or [])
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])

        latency_ms = int((perf_counter() - started) * 1000)
        tool_calls = tuple(self._finalize_tool_calls(accumulated))
        for call in tool_calls:
            if on_tool_call is not None:
                await on_tool_call(call)
        resolved_usage, usage_estimated = self._resolve_usage(usage, messages, content + reasoning)
        return self._result(
            model=model,
            message=ChatMessage.assistant(content, tool_calls, reasoning_content=reasoning),
            finish_reason=finish_reason,
            usage=resolved_usage,
            usage_estimated=usage_estimated,
            latency_ms=latency_ms,
            raw=None,
        )

    # ---- 内部：HTTP ----
    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.post(self._url(path), json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"Upstream timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailableError(
                f"Cannot reach model provider '{self.config.name}'", details={"reason": type(exc).__name__}
            ) from exc
        if response.status_code >= 400:
            await self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelProviderUnavailableError("Upstream returned a non-JSON body") from exc
        return body if isinstance(body, dict) else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", **dict(self.config.headers)}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        params: Mapping[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        merged = {**dict(self.config.default_params), **dict(params or {})}
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.to_openai() for message in messages],
            "stream": stream,
        }
        for key in PARAM_WHITELIST:
            if merged.get(key) is not None:
                payload[key] = merged[key]
        # 4.1.2 第 4 条：未绑定工具时不下发 tools（部分兼容实现会因此报错）
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        return payload

    # ---- 内部：解析与错误映射 ----
    async def _iter_chunks(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        """按 `data: ` 分帧解析 SSE；`[DONE]` 结束（4.1.2 第 3 条）。"""
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            if raw == "[DONE]":
                return
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("llm.stream_bad_chunk", provider=self.config.name)
                continue
            if isinstance(chunk, dict):
                yield chunk

    @staticmethod
    def _accumulate_tool_calls(accumulated: dict[int, dict[str, str]], items: list[Any]) -> None:
        """按 `index` 聚合分片的 `tool_calls`（4.1.2 第 3 条）。"""
        for item in items:
            if not isinstance(item, dict):
                continue
            index = int(item.get("index") or 0)
            slot = accumulated.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                slot["id"] = str(item["id"])
            function = item.get("function") or {}
            if function.get("name"):
                # 名称通常只出现在首个分片，重复出现时以首个为准
                slot["name"] = slot["name"] or str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])

    @staticmethod
    def _finalize_tool_calls(accumulated: dict[int, dict[str, str]]) -> list[ToolCallSpec]:
        calls: list[ToolCallSpec] = []
        for index in sorted(accumulated):
            slot = accumulated[index]
            arguments, raw = parse_arguments(slot["arguments"])
            calls.append(
                ToolCallSpec(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments,
                    raw_arguments=raw,
                )
            )
        return calls

    @staticmethod
    def _parse_tool_calls(items: list[Any]) -> list[ToolCallSpec]:
        """非流式响应的 `message.tool_calls`（已完整，不需要拼接）。"""
        calls: list[ToolCallSpec] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            arguments, raw = parse_arguments(str(function.get("arguments") or ""))
            calls.append(
                ToolCallSpec(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    raw_arguments=raw,
                )
            )
        return calls

    @staticmethod
    def _usage_from_payload(payload: Mapping[str, Any]) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
        )

    def _resolve_usage(
        self,
        usage: Mapping[str, Any] | TokenUsage | None,
        messages: list[ChatMessage],
        content: str,
    ) -> tuple[TokenUsage, bool]:
        """上游 `usage` 缺失时本地估算（4.1.2 第 6 条），返回 `(usage, usage_estimated)`。"""
        if isinstance(usage, TokenUsage):
            if usage.total_tokens > 0:
                return usage, False
        elif usage is not None:
            parsed = self._usage_from_payload(usage)
            if parsed.total_tokens > 0:
                return parsed, False
        prompt_tokens = sum(estimate_tokens(message.content) for message in messages)
        return TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=estimate_tokens(content)), True

    def _result(
        self,
        *,
        model: str,
        message: ChatMessage,
        finish_reason: str,
        usage: TokenUsage,
        usage_estimated: bool,
        latency_ms: int,
        raw: dict[str, Any] | None,
    ) -> LLMResult:
        cost: Decimal = estimate_cost(model, usage.prompt_tokens, usage.completion_tokens, self._prices)
        return LLMResult(
            message=message,
            finish_reason=finish_reason,
            usage=usage,
            model_name=model,
            latency_ms=latency_ms,
            cost_usd=cost,
            cost_estimated=has_price(model, self._prices),
            usage_estimated=usage_estimated,
            raw=raw,
        )

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """把上游状态码翻译成 `MODEL_*`（1.6 的映射规则 + 附录 A）。"""
        status = response.status_code
        try:
            # 流式分支此时 body 还没读（`response.json()` 会先抛 `ResponseNotRead`）；
            # 先把错误体读出来，4xx/5xx 才能按附录 A 映射，而不是漏成未捕获异常。
            await response.aread()
            body = response.json()
        except (ValueError, httpx.HTTPError):
            body = {}
        # DeepSeek 的网关（governor）在鉴权失败时回的是**纯文本**（"Authentication Fails (governor)"）：
        # 只认 JSON 会把上游的原话丢掉，排障时分不清是 key 错还是余额不足。
        message = _extract_error_message(body) or _plain_text_body(response) or f"upstream returned HTTP {status}"
        details = {"provider": self.config.name, "upstream_status": status, "upstream_message": message}
        lowered = message.lower()
        if status == 402:
            # DeepSeek 402 - Insufficient Balance：账户没钱，重试与改参数都无用
            raise ModelAuthFailedError(
                "Model provider rejected the request: insufficient balance. Top up the account or switch provider",
                details=details,
            )
        if status in (401, 403):
            raise ModelAuthFailedError(
                "Model provider rejected the credentials. Check the provider api_key.", details=details
            )
        if status == 429:
            raise ModelRateLimitedError("Model provider rate limit exceeded", details=details)
        if status >= 500:
            raise ModelProviderUnavailableError("Model provider is unavailable", details=details)
        if any(hint in lowered for hint in CONTEXT_OVERFLOW_HINTS):
            raise ModelContextOverflowError("Context exceeds the model window", details=details)
        raise ModelBadRequestError(
            "Model provider rejected the request. Check model name and parameters.", details=details
        )


MAX_UPSTREAM_MESSAGE_CHARS = 300
"""`details.upstream_message` 的长度上限（上游可能回一整页 HTML）。"""


def _plain_text_body(response: httpx.Response) -> str:
    """非 JSON 错误体的可读文本（压平空白 + 截断）；读不出来就返回空串。"""
    try:
        text = " ".join(response.text.split())
    except (UnicodeDecodeError, ValueError):
        return ""
    if len(text) > MAX_UPSTREAM_MESSAGE_CHARS:
        return f"{text[:MAX_UPSTREAM_MESSAGE_CHARS]}…"
    return text


def _extract_error_message(body: Any) -> str:
    """从 OpenAI 风格的错误体里取可读信息（`{"error": {"message": ...}}`）。"""
    if not isinstance(body, Mapping):
        return ""
    error = body.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or "")
    if isinstance(error, str):
        return error
    return str(body.get("message") or "")
