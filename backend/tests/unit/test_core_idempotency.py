"""`Idempotency-Key` 存储（详细设计 1.5.5：内存 LRU + TTL 10 分钟）。"""

from __future__ import annotations

from app.core.idempotency import IdempotencyStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_put_then_get_returns_snapshot() -> None:
    store = IdempotencyStore(clock=FakeClock())
    assert store.get("agents.create", "k1") is None

    store.put("agents.create", "k1", status_code=201, payload={"data": {"id": "a1"}})
    cached = store.get("agents.create", "k1")
    assert cached is not None
    assert (cached.status_code, cached.payload) == (201, {"data": {"id": "a1"}})


def test_scope_isolates_keys() -> None:
    store = IdempotencyStore(clock=FakeClock())
    store.put("agents.create", "same", status_code=201, payload={"x": 1})
    assert store.get("conversations.create", "same") is None


def test_entries_expire_after_ttl() -> None:
    clock = FakeClock()
    store = IdempotencyStore(ttl_seconds=600, clock=clock)
    store.put("agents.create", "k1", status_code=201, payload={"x": 1})
    clock.now = 599
    assert store.get("agents.create", "k1") is not None
    clock.now = 601
    assert store.get("agents.create", "k1") is None


def test_lru_evicts_oldest_entry() -> None:
    store = IdempotencyStore(max_entries=2, clock=FakeClock())
    store.put("s", "a", status_code=201, payload={})
    store.put("s", "b", status_code=201, payload={})
    store.put("s", "c", status_code=201, payload={})
    assert store.get("s", "a") is None
    assert store.get("s", "c") is not None


def test_clear_empties_store() -> None:
    store = IdempotencyStore(clock=FakeClock())
    store.put("s", "a", status_code=201, payload={})
    store.clear()
    assert store.get("s", "a") is None
