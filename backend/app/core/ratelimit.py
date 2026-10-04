"""进程内令牌桶限流（详细设计 1.5.4；Phase 2 落点）。

- `llm:{provider_id}`：默认 5 req/s；`tool:{tool_id}`：默认 10 req/s（1.5.4）；
- **纯内存实现**：不引入 Redis（SD-3 的"单机 + 本地"前提）；
- 用法：`async with limiter.limit(f"tool:{tool_id}"): ...`；
- 单会话最多 1 个活跃 Run 的约束由 `services/run_service.RunRegistry` 负责，
  不在本模块（1.5.4 第 3 条）；
- 速率不做成 `Settings` 字段：附录 C 没有对应环境变量，
  `tests/unit/test_config_docs_consistency.py` 要求二者逐项一致（1.4 / 附录 C）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic

DEFAULT_RATE_PER_SECOND = 10.0
"""默认速率（1.5.4 的 `tool:{tool_id}` 取值）。"""

LLM_RATE_PER_SECOND = 5.0
"""`llm:{provider_id}` 的默认速率（1.5.4；Phase 1 的 Provider 直连未接入，留待需要时启用）。"""

CAPACITY_MULTIPLIER = 2.0
"""桶容量 = 速率 × 该系数：允许短暂突发，但不至于长时间积压（1.5.4）。"""


@dataclass(slots=True)
class TokenBucket:
    """单个 key 的令牌桶。"""

    rate_per_second: float
    tokens: float
    updated_at: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def capacity(self) -> float:
        return max(1.0, self.rate_per_second * CAPACITY_MULTIPLIER)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
        self.updated_at = now

    def take(self, now: float) -> float:
        """尝试取 1 个令牌；取不到时返回需要等待的秒数（0 表示已取到）。"""
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate_per_second


class TokenBucketLimiter:
    """按 key 分桶的限流器（进程内，单事件循环）。"""

    def __init__(self, *, default_rate_per_second: float = DEFAULT_RATE_PER_SECOND) -> None:
        self._default_rate = default_rate_per_second
        self._rates: dict[str, float] = {}
        self._buckets: dict[str, TokenBucket] = {}

    def configure(self, key_or_prefix: str, rate_per_second: float) -> None:
        """覆盖某个 key（或 `前缀:` 形态）的速率。"""
        self._rates[key_or_prefix] = rate_per_second

    def rate_for(self, key: str) -> float:
        """取 key 的速率：精确 key > 前缀（`tool` / `llm`）> 默认值。"""
        if key in self._rates:
            return self._rates[key]
        prefix = key.split(":", 1)[0]
        return self._rates.get(prefix, self._default_rate)

    def _bucket(self, key: str) -> TokenBucket:
        rate = self.rate_for(key)
        bucket = self._buckets.get(key)
        if bucket is None or bucket.rate_per_second != rate:
            bucket = TokenBucket(rate_per_second=rate, tokens=rate)
            self._buckets[key] = bucket
        return bucket

    async def acquire(self, key: str) -> None:
        """取 1 个令牌，不足则排队等待（同 key 串行，符合"限速"语义）。"""
        bucket = self._bucket(key)
        async with bucket.lock:
            while True:
                delay = bucket.take(monotonic())
                if delay <= 0.0:
                    return
                await asyncio.sleep(min(delay, 1.0))

    @asynccontextmanager
    async def limit(self, key: str) -> AsyncIterator[None]:
        """`async with limiter.limit("tool:calculator"):` 形态（4.2.3 步骤 6）。"""
        await self.acquire(key)
        yield

    def reset(self) -> None:
        """清空桶（测试用）。"""
        self._buckets.clear()
