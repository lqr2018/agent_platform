"""超时 / 重试工具（详细设计 1.5.3）。

- `@async_retry(...)`：指数退避装饰器，退避节奏 `base_delay * 2^(attempt-1)`（默认 0.5/1/2s）；
- `call_with_retry(...)`：同样的策略，但用于**运行期才知道次数**的场景（如 `LLM_MAX_RETRIES`）；
- `should_retry`：允许按异常 + 已尝试次数决定是否继续重试 —— LLM 用它实现
  "首 token 之后不再重试"（1.5.3 的补充约定）。

**禁止在业务代码里裸写 try/sleep 循环**（1.5.3）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_BASE_DELAY_SECONDS = 0.5
DEFAULT_MAX_DELAY_SECONDS = 8.0

RetryPredicate = Callable[[BaseException, int], bool]


def _delay_for(attempt: int, base_delay: float, max_delay: float) -> float:
    """第 `attempt` 次失败后的等待秒数（1-based）。"""
    delay: float = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay


async def call_with_retry(
    factory: Callable[[], Awaitable[R]],
    *,
    times: int,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    should_retry: RetryPredicate | None = None,
    event: str = "call",
) -> R:
    """执行 `factory()`，失败时按 1.5.3 的退避策略重试 `times` 次。"""
    attempts = max(1, times + 1)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts or (should_retry is not None and not should_retry(exc, attempt)):
                raise
            delay = _delay_for(attempt, base_delay, max_delay)
            logger.warning(
                "retry.scheduled",
                operation=event,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=delay,
                error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay)
    # 理论不可达：循环内要么 return 要么 raise
    raise last_error if last_error is not None else RuntimeError("retry loop exited without result")


def async_retry(
    *,
    times: int,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """装饰器形态（1.5.3 的 `core/retry.py`）。"""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await call_with_retry(
                lambda: func(*args, **kwargs),
                times=times,
                base_delay=base_delay,
                max_delay=max_delay,
                retry_on=retry_on,
                event=func.__name__,
            )

        return wrapper

    return decorator
