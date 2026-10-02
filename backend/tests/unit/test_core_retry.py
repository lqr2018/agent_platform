"""重试与退避（详细设计 1.5.3）。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.retry import async_retry, call_with_retry


class BoomError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_call_with_retry_succeeds_after_failures() -> None:
    attempts: list[int] = []

    async def factory() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise BoomError("nope")
        return "ok"

    result = await call_with_retry(factory, times=2, base_delay=0.001, retry_on=(BoomError,))
    assert result == "ok"
    assert len(attempts) == 3  # 首次 + 2 次重试


@pytest.mark.asyncio
async def test_call_with_retry_gives_up_after_budget() -> None:
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)
        raise BoomError("always")

    with pytest.raises(BoomError):
        await call_with_retry(factory, times=1, base_delay=0.001, retry_on=(BoomError,))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_should_retry_can_stop_early() -> None:
    """1.5.3：LLM 首 token 之后不再重试 —— 用 `should_retry` 表达。"""
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)
        raise BoomError("after first token")

    with pytest.raises(BoomError):
        await call_with_retry(
            factory,
            times=3,
            base_delay=0.001,
            retry_on=(BoomError,),
            should_retry=lambda _exc, _attempt: False,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_retryable_error_is_not_retried() -> None:
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        await call_with_retry(factory, times=3, base_delay=0.001, retry_on=(BoomError,))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_retried() -> None:
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await call_with_retry(factory, times=3, base_delay=0.001)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_retry_decorator() -> None:
    calls: list[int] = []

    @async_retry(times=1, base_delay=0.001, retry_on=(BoomError,))
    async def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise BoomError("once")
        return "done"

    assert await flaky() == "done"
    assert len(calls) == 2
