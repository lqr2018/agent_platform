"""停止条件与终态映射（详细设计 4.4.3 的"停止/终止条件汇总"）。

纯函数集合（无 IO、无状态），每一步循环前调用一次，便于单测覆盖全部分支：

| 条件 | 结果 |
|---|---|
| `finish_reason=stop` 且无 tool_calls | `succeeded` |
| `step_index` 达 `max_steps` | `failed` / `MODEL_MAX_STEPS_EXCEEDED` |
| 时长超 `agent.timeout_seconds` | `failed` / `RUN_TIMEOUT` |
| 用户取消 | `canceled` / `RUN_CANCELED` |
| LLM 错误且重试耗尽 | `failed` / `MODEL_*`（由 Provider 抛出） |
| 同一工具连续失败 3 次 | `failed` / `TOOL_REPEATED_FAILURE`（Phase 2 生效） |
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.core.enums import RunStatus
from app.core.errors import (
    AppError,
    ModelMaxStepsExceededError,
    RunCanceledError,
    RunTimeoutError,
    ToolRepeatedFailureError,
)

MAX_CONSECUTIVE_TOOL_FAILURES = 3
"""同一工具连续失败 3 次 → Run 失败（4.4.3）。"""


class CancelSignal(Protocol):
    """最小取消信号（`asyncio.Event` 满足；避免 runtime 与具体实现耦合）。"""

    def is_set(self) -> bool: ...


class StopReason(StrEnum):
    """导致循环结束的原因（映射到 `runs.status` + `error_code`）。

    `REPEATED_TOOL_FAILURE` 是 Phase 2 的补充取值（4.4.3：同一工具连续失败 3 次），
    由 `AgentRuntime` 在工具分支判定后直接用 `error_for()` 抛出。
    """

    CANCELED = "canceled"
    TIMEOUT = "timeout"
    MAX_STEPS = "max_steps"
    REPEATED_TOOL_FAILURE = "repeated_tool_failure"


def exceeded_max_steps(step_index: int, max_steps: int) -> bool:
    return step_index >= max_steps


def exceeded_timeout(started_at: datetime, timeout_seconds: float, *, now: datetime) -> bool:
    if timeout_seconds <= 0:
        return False
    return (now - started_at).total_seconds() > timeout_seconds


def repeated_tool_failure(recent_tool_failures: Mapping[str, int], tool_name: str) -> bool:
    """4.4.3：同一工具**连续**失败 3 次。"""
    return recent_tool_failures.get(tool_name, 0) >= MAX_CONSECUTIVE_TOOL_FAILURES


def evaluate(
    *,
    canceled: bool,
    step_index: int,
    max_steps: int,
    started_at: datetime,
    now: datetime,
    timeout_seconds: float,
) -> StopReason | None:
    """循环开始前的统一判定（返回 `None` 表示可以继续）。"""
    if canceled:
        return StopReason.CANCELED
    if exceeded_timeout(started_at, timeout_seconds, now=now):
        return StopReason.TIMEOUT
    if exceeded_max_steps(step_index, max_steps):
        return StopReason.MAX_STEPS
    return None


def terminal_status(reason: StopReason) -> RunStatus:
    """`StopReason` → `runs.status`（2.6 的状态机）。"""
    return RunStatus.CANCELED if reason is StopReason.CANCELED else RunStatus.FAILED


def error_for(reason: StopReason) -> AppError:
    """`StopReason` → 具体异常（同时给出 `error_code`）。"""
    if reason is StopReason.CANCELED:
        return RunCanceledError()
    if reason is StopReason.TIMEOUT:
        return RunTimeoutError()
    if reason is StopReason.REPEATED_TOOL_FAILURE:
        return ToolRepeatedFailureError(
            f"A tool failed {MAX_CONSECUTIVE_TOOL_FAILURES} times in a row",
            details={"max_consecutive_failures": MAX_CONSECUTIVE_TOOL_FAILURES},
        )
    return ModelMaxStepsExceededError()
