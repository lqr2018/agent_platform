"""`FakeLLMProvider`（详细设计 4.1.3 的行为契约，Phase 1–7 的测试底座）。

契约（判定依据是最后一条 user 消息，**必须稳定**）：

| 条件 | 行为 |
|---|---|
| `params.simulate_error` 或最后一条 user 消息含 `simulate_error=<code>` | 抛对应 `MODEL_*` |
| 含 `计算` / `calculate` 且 `tools` 里有 `calculator` | `tool_calls=[calculator(expr)]`（Phase 2 用） |
| 含 `检索` 且 `tools` 里有 `kb_search` | `tool_calls=[kb_search(query)]`（Phase 5 用） |
| messages 里已有 `role=tool` | `finish_reason=stop`，内容 `结果：{工具结果}` |
| 其他 | `FAKE_RESPONSE: {user 文本}`，`usage = TokenUsage(10, 5)` |

`stream=True` 时按 `chunk_size` 切片调用 `on_delta`（用真实流式路径覆盖 SSE 渲染）。
"第二轮判定"只在首轮已产生工具调用时才可能为真 —— 没有 `tools` 时（Phase 1）永远走最后一行。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from decimal import Decimal
from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.core.enums import MessageRole
from app.core.errors import (
    AppError,
    FeatureNotImplementedError,
    ModelAuthFailedError,
    ModelBadRequestError,
    ModelContextOverflowError,
    ModelProviderUnavailableError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from app.runtime.llm.base import (
    ChatMessage,
    DeltaCallback,
    LLMResult,
    ProviderConfig,
    ToolCallCallback,
    ToolCallSpec,
)
from app.runtime.llm.usage import TokenUsage
from app.runtime.observability.tracer import Span

DEFAULT_CHUNK_SIZE = 4
"""流式切片的字符数（4.1.3：让前端/Trace 看到多段 `message.delta`）。"""

SIMULATE_ERROR_PATTERN = re.compile(r"simulate_error\s*=\s*([a-z_]+)")

SIMULATED_ERRORS: Mapping[str, type[AppError]] = {
    "timeout": ModelTimeoutError,
    "auth": ModelAuthFailedError,
    "bad_request": ModelBadRequestError,
    "rate_limited": ModelRateLimitedError,
    "unavailable": ModelProviderUnavailableError,
    "context_overflow": ModelContextOverflowError,
}
"""`simulate_error` 的取值 → 异常类（4.1.3 的 Phase 1 约定）。"""

EXPRESSION_PATTERN = re.compile(r"[-+*/%().\d\s]{3,}")
CALCULATE_HINTS = ("计算", "calculate", "compute")


class FakeLLMProvider:
    """确定性测试替身（只在 `APP_ENV=test` 时由 registry 注册）。"""

    name = "fake"

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        delay_seconds: float = 0.0,
    ) -> None:
        self.config = config or ProviderConfig(id="fake", name="fake", kind="fake", base_url="fake://")
        self._chunk_size = max(1, chunk_size)
        self._delay = delay_seconds

    @classmethod
    def from_settings(cls, config: ProviderConfig, settings: Settings) -> FakeLLMProvider:
        """registry 的工厂签名（fake 不需要 `settings`，保留以便统一实例化）。"""
        return cls(config)

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
        started = perf_counter()
        user_message = _last_user_message(messages)
        # 4.1.2 第 5 条：provider `default_params` 与本次参数浅合并（本次优先）
        effective_params = {**dict(self.config.default_params), **dict(params or {})}
        simulated = _simulate_error(effective_params, user_message)
        if simulated is not None:
            raise simulated

        tool_calls = _tool_calls(user_message, tools or [], messages)
        if tool_calls:
            content = ""
            finish_reason = "tool_calls"
        elif _has_tool_result(messages):
            content = f"结果：{_last_tool_content(messages)}"
            finish_reason = "stop"
        else:
            content = f"FAKE_RESPONSE: {user_message}"
            finish_reason = "stop"

        if stream and on_delta is not None and content:
            for chunk in _chunks(content, self._chunk_size):
                if self._delay:
                    await asyncio.sleep(self._delay)
                await on_delta(chunk)
        for call in tool_calls:
            if on_tool_call is not None:
                await on_tool_call(call)

        return LLMResult(
            message=ChatMessage.assistant(content, tuple(tool_calls)),
            finish_reason=finish_reason,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            model_name=model,
            latency_ms=int((perf_counter() - started) * 1000),
            cost_usd=Decimal(0),
            cost_estimated=False,
            usage_estimated=False,
            raw={"provider": "fake"},
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        """4.1.3 的 `FakeEmbeddingProvider` 属 Phase 5（`rag/embedders/fake.py`）。"""
        raise FeatureNotImplementedError(
            "Fake embedding provider lands in Phase 5 (rag/embedders/fake.py)",
            details={"provider": self.name},
        )


def _last_user_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == MessageRole.USER:
            return message.content
    return ""


def _has_tool_result(messages: list[ChatMessage]) -> bool:
    return any(message.role == MessageRole.TOOL for message in messages)


def _last_tool_content(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == MessageRole.TOOL:
            return message.content
    return ""


def _simulate_error(params: Mapping[str, Any] | None, user_message: str) -> AppError | None:
    """`params.simulate_error` 优先，其次扫最后一条 user 消息（4.1.3 的 Phase 1 约定）。"""
    code = str((params or {}).get("simulate_error") or "").strip()
    if not code:
        match = SIMULATE_ERROR_PATTERN.search(user_message)
        code = match.group(1) if match else ""
    error_class = SIMULATED_ERRORS.get(code) if code else None
    if error_class is None:
        return None
    return error_class(f"Simulated upstream error: {code}", details={"simulate_error": code})


def _tool_calls(user_message: str, tools: list[dict[str, Any]], messages: list[ChatMessage]) -> list[ToolCallSpec]:
    """首轮才产生 tool_calls（第二轮已有 tool 结果，按 4.1.3 直接给结论）。"""
    if _has_tool_result(messages) or not user_message:
        return []
    names = {str((tool.get("function") or {}).get("name") or tool.get("name") or "") for tool in tools}
    if "calculator" in names and any(hint in user_message for hint in CALCULATE_HINTS):
        return [
            ToolCallSpec(id="call_1", name="calculator", arguments={"expression": _extract_expression(user_message)})
        ]
    if "kb_search" in names and "检索" in user_message:
        return [ToolCallSpec(id="call_1", name="kb_search", arguments={"query": user_message})]
    return []


def _extract_expression(text: str) -> str:
    """从自然语言里取最长的算式（4.1.3：参数从文本中提取算式）。"""
    candidates = [match.group(0).strip() for match in EXPRESSION_PATTERN.finditer(text)]
    scored = [item for item in candidates if any(char.isdigit() for char in item)]
    return max(scored, key=len) if scored else "0"


def _chunks(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]
