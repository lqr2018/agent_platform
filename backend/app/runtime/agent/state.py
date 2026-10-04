"""Agent 运行时状态与入参快照（详细设计 4.4.1）。

`AgentSpec` 是 Agent 配置的**不可变快照**：服务层从 `agents` 行装配后传入，
runtime 全程只读它（1.2：runtime 不碰 ORM）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.enums import RunStatus
from app.core.errors import AppError
from app.runtime.llm.base import ChatMessage
from app.runtime.llm.usage import ZERO_USAGE, TokenUsage
from app.runtime.observability.tracer import utcnow
from app.runtime.tools.base import ToolDefinition

DEFAULT_MAX_STEPS = 8
DEFAULT_TIMEOUT_SECONDS = 180


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Agent 配置快照（4.4.1）：model / prompt / tools / kb / memory / workflow。"""

    id: str
    name: str
    model_provider_id: str
    model_name: str
    system_prompt: str = ""
    prompt_version: int = 1
    model_params: Mapping[str, Any] = field(default_factory=dict)
    tool_ids: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    memory_config: Mapping[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    status: str = "enabled"
    tags: tuple[str, ...] = ()

    @property
    def short_term_config(self) -> Mapping[str, Any]:
        """`memory_config.short_term`（MVP 唯一生效的记忆段，SD-15 / 4.3.1）。"""
        short_term = self.memory_config.get("short_term")
        return short_term if isinstance(short_term, Mapping) else {}


@dataclass(slots=True)
class AgentState:
    """一次 Run 的可变状态（4.4.1 的 `AgentState`）。"""

    run_id: str
    trace_id: str
    agent: AgentSpec
    conversation_id: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    step_index: int = 0
    usage: TokenUsage = ZERO_USAGE
    cost_usd: Decimal = Decimal(0)
    message_id: str | None = None
    """最后一次 assistant 消息的行 id（供 `runs.output.message_id` 与 `RunResult`）。"""
    tool_definitions: list[ToolDefinition] = field(default_factory=list)
    """本步骤**可见**的工具定义（4.2.2：每一步重新计算，`tool_ids` 顺序即下发顺序）。"""
    tool_call_count: int = 0
    calls_per_tool: dict[str, int] = field(default_factory=dict)
    """本 Run 内每个工具的已调用次数（4.2.3 步骤 5 的次数闸门，跨 step 累积）。"""
    recent_tool_failures: dict[str, int] = field(default_factory=dict)
    """同一工具**连续**失败次数（4.4.3：达 3 次 → `TOOL_REPEATED_FAILURE`）。"""
    finish_reason: str | None = None
    error: AppError | None = None
    started_at: datetime = field(default_factory=utcnow)

    def reset_context(self, messages: list[ChatMessage]) -> None:
        """每一步都重新组装上下文（4.4.2）：历史消息本身不被修改，保证可重放。"""
        self.messages = list(messages)


@dataclass(slots=True)
class RunResult:
    """一次 Run 的终态（`AgentRuntime.run()` 的返回值）。"""

    run_id: str
    trace_id: str
    status: RunStatus
    output_text: str = ""
    message_id: str | None = None
    steps: int = 0
    tool_call_count: int = 0
    usage: TokenUsage = ZERO_USAGE
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCEEDED
