"""停止条件与终态映射（详细设计 4.4.3 的"停止/终止条件汇总"）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.enums import RunStatus
from app.core.errors import ErrorCode, ModelMaxStepsExceededError, RunCanceledError, RunTimeoutError
from app.runtime.agent.stop import (
    MAX_CONSECUTIVE_TOOL_FAILURES,
    StopReason,
    error_for,
    evaluate,
    exceeded_max_steps,
    exceeded_timeout,
    repeated_tool_failure,
    terminal_status,
)

STARTED = datetime(2026, 10, 1, tzinfo=UTC)


def test_exceeded_max_steps() -> None:
    assert not exceeded_max_steps(0, 8)
    assert not exceeded_max_steps(7, 8)
    assert exceeded_max_steps(8, 8)


def test_exceeded_timeout() -> None:
    assert not exceeded_timeout(STARTED, 180, now=STARTED + timedelta(seconds=179))
    assert exceeded_timeout(STARTED, 180, now=STARTED + timedelta(seconds=181))
    assert not exceeded_timeout(STARTED, 0, now=STARTED + timedelta(days=1))  # 0 = 不限


def test_repeated_tool_failure() -> None:
    assert not repeated_tool_failure({"calculator": 2}, "calculator")
    assert repeated_tool_failure({"calculator": MAX_CONSECUTIVE_TOOL_FAILURES}, "calculator")


def test_evaluate_priority() -> None:
    base = {"step_index": 0, "max_steps": 8, "started_at": STARTED, "now": STARTED, "timeout_seconds": 180}
    assert evaluate(canceled=False, **base) is None
    assert evaluate(canceled=True, **base) is StopReason.CANCELED
    assert evaluate(canceled=False, **{**base, "step_index": 8}) is StopReason.MAX_STEPS
    assert evaluate(canceled=False, **{**base, "now": STARTED + timedelta(seconds=200)}) is StopReason.TIMEOUT


def test_terminal_status_and_error_mapping() -> None:
    assert terminal_status(StopReason.CANCELED) is RunStatus.CANCELED
    assert terminal_status(StopReason.MAX_STEPS) is RunStatus.FAILED
    assert terminal_status(StopReason.TIMEOUT) is RunStatus.FAILED

    assert isinstance(error_for(StopReason.CANCELED), RunCanceledError)
    assert isinstance(error_for(StopReason.TIMEOUT), RunTimeoutError)
    assert isinstance(error_for(StopReason.MAX_STEPS), ModelMaxStepsExceededError)

    assert error_for(StopReason.CANCELED).code is ErrorCode.RUN_CANCELED
    assert error_for(StopReason.TIMEOUT).code is ErrorCode.RUN_TIMEOUT
    assert error_for(StopReason.MAX_STEPS).code is ErrorCode.MODEL_MAX_STEPS_EXCEEDED
