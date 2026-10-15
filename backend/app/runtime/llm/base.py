"""LLM Provider 抽象层（详细设计 4.1.1 / 4.1.3）。

`runtime/**` 不 import `db/models` / `services`（1.2）：服务层把 `model_providers` 行装配成
`ProviderConfig` 快照传进来，Provider 只认快照 + `Settings`。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.core.enums import MessageRole
from app.runtime.llm.usage import TokenUsage
from app.runtime.observability.tracer import Span


@dataclass(frozen=True, slots=True)
class ToolCallSpec:
    """LLM 发起的一次工具调用（4.1.1）。

    `arguments` 为已 `json.loads` 的字典；解析失败时 `arguments={}` 且原始文本留在 `raw_arguments`
    （Phase 2 的 `tool_invocations.arguments` 需要"未清洗"的原貌）。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """发给 LLM / 从 LLM 返回的一条消息（4.1.1）。"""

    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCallSpec, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str = ""
    """思考型模型（DeepSeek 的 thinking 模式）的思维链，**与 `content` 同级**（4.1.2 第 8 条）。

    上游给了才有值。带 `tools` 的多轮对话必须把历史轮次的 CoT 回传给上游（见 `to_openai()`），
    不带 `tools` 时上游会忽略它；对不支持该字段的 Provider 我们永不主动添加（空 = 不出现在报文里）。
    """

    @staticmethod
    def system(content: str) -> ChatMessage:
        return ChatMessage(role=MessageRole.SYSTEM, content=content)

    @staticmethod
    def user(content: str) -> ChatMessage:
        return ChatMessage(role=MessageRole.USER, content=content)

    @staticmethod
    def assistant(
        content: str = "",
        tool_calls: tuple[ToolCallSpec, ...] = (),
        *,
        reasoning_content: str = "",
    ) -> ChatMessage:
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )

    @staticmethod
    def tool(content: str, *, tool_call_id: str, name: str | None = None) -> ChatMessage:
        return ChatMessage(role=MessageRole.TOOL, content=content, tool_call_id=tool_call_id, name=name)

    def to_openai(self) -> dict[str, Any]:
        """OpenAI Chat Completions 的消息结构（4.1.2 第 1 条 + 第 8 条）。"""
        payload: dict[str, Any] = {"role": str(self.role), "content": self.content}
        if self.reasoning_content:
            # 只有上游给过 CoT 时才回传：带 tools 的多轮对话上游要求回传，
            # 不带 tools 时上游会忽略；其它 Provider 收不到这个非标字段（空值不下发）。
            payload["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments or dump_arguments(call.arguments)},
                }
                for call in self.tool_calls
            ]
            # assistant 只调工具时 content 可为空（2.6）
            if not self.content:
                payload["content"] = None
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload


@dataclass(slots=True)
class LLMResult:
    """一次 `chat()` 的结果（4.1.1 + 4.8.3 的成本字段）。"""

    message: ChatMessage
    finish_reason: str
    usage: TokenUsage
    model_name: str
    latency_ms: int
    cost_usd: Decimal = Decimal(0)
    cost_estimated: bool = False
    usage_estimated: bool = False
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider 配置快照（服务层装配；1.2：runtime 不碰 ORM）。"""

    id: str
    name: str
    kind: str
    base_url: str
    api_key: str = ""
    default_model: str | None = None
    models: tuple[Mapping[str, Any], ...] = ()
    default_params: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    def model_names(self) -> tuple[str, ...]:
        """白名单里的模型名（2.3 的 `models[]`）。"""
        return tuple(str(item.get("name")) for item in self.models if item.get("name"))


DeltaCallback = Callable[[str], Awaitable[None]]
ToolCallCallback = Callable[[ToolCallSpec], Awaitable[None]]


class LLMProvider(Protocol):
    """4.1.1 的 Provider 协议：`chat` 是唯一对话入口（流式与非流式共用）。"""

    name: str

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
    ) -> LLMResult: ...

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...


def dump_arguments(arguments: Mapping[str, Any]) -> str:
    """把 arguments 还原为 JSON 文本（`tool_calls.function.arguments` 需要字符串，4.1.2 第 3 条）。"""
    return json.dumps(dict(arguments), ensure_ascii=False)


def parse_arguments(raw: str) -> tuple[dict[str, Any], str]:
    """解析 `function.arguments`；失败时返回 `({}, raw)`（4.1.1：解析失败不丢原文）。"""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, raw
    if not isinstance(parsed, dict):
        return {}, raw
    return parsed, raw
