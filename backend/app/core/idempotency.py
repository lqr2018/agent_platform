"""`Idempotency-Key` 支持（详细设计 1.5.5 / 3.1）。

范围与理由：

- MVP **不引入 Redis**（SD-14 红线②）：用进程内 LRU + TTL 10 分钟保存"首次响应快照"，
  重复提交同一个 key 时直接回放首次结果（含状态码），不重复写库；
- 真正的兜底仍在数据库：`messages` 的 `(conversation_id, seq)` UNIQUE 等约束（1.5.5）；
- 键按 `scope` 隔离（`agents.create` / `model_providers.create` / `conversations.create` …），
  避免同一个 key 在不同端点上误命中。

**不适用于 SSE 端点**：`POST /conversations/{id}/messages` 的响应是流，无法回放；
重复提交由 1.5.4 的"单会话最多 1 个活跃 Run → `CONFLICT`"承担（已在 `run_service` 实现）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

DEFAULT_TTL_SECONDS = 600.0
"""TTL 10 分钟（1.5.5）。"""

DEFAULT_MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """首次响应的快照（`payload` 为已序列化的响应体 dict）。"""

    status_code: int
    payload: dict[str, Any]


class IdempotencyStore:
    """进程内 LRU（线程安全；测试可注入时钟以保证确定性）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, CachedResponse]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def scoped_key(scope: str, key: str) -> str:
        """`scope` + 原始 key（1.5.5 的键隔离）。"""
        return f"{scope}\x00{key}"

    def get(self, scope: str, key: str) -> CachedResponse | None:
        """命中则返回首次响应快照；过期条目顺带清理。"""
        with self._lock:
            entry = self._entries.get(self.scoped_key(scope, key))
            if entry is None:
                return None
            stored_at, response = entry
            if self._clock() - stored_at > self._ttl:
                self._entries.pop(self.scoped_key(scope, key), None)
                return None
            self._entries.move_to_end(self.scoped_key(scope, key))
            return response

    def put(self, scope: str, key: str, *, status_code: int, payload: dict[str, Any]) -> None:
        """记录首次响应（超出容量时淘汰最久未使用的条目）。"""
        with self._lock:
            composed = self.scoped_key(scope, key)
            self._entries[composed] = (self._clock(), CachedResponse(status_code, payload))
            self._entries.move_to_end(composed)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_store = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    """进程内单例（`api/deps.py` 转出为依赖）。"""
    return _store
