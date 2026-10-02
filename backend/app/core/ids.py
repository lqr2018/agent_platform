"""ID 生成（一致性契约 0.2.1）。

| 用途 | 形态 | 生成方式 |
|---|---|---|
| 主键 `id` | 26 位 ULID（Crockford Base32，时间有序） | `new_ulid()` |
| `trace_id` | 32 位小写十六进制 | `new_trace_id()`（`uuid4().hex`） |
| `span_id` | 16 位小写十六进制 | `new_span_id()` |
| `request_id` | 32 位小写十六进制 | `new_request_id()` |

ULID 按官方规范的 Monotonicity 条款实现：**同一毫秒内递增随机段**（进程内加锁），
因此 `new_ulid()` 生成的字符串在同一进程内严格递增 —— 字符串排序即时间排序。
"""

from __future__ import annotations

import secrets
import threading
import time
from uuid import uuid4

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
"""Crockford Base32：去掉易混淆的 I / L / O / U。"""

ULID_LENGTH = 26
TIMESTAMP_CHARS = 10
"""48 位毫秒时间戳用 10 个 Base32 字符表示（50 位，高 2 位恒为 0）。"""

_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1

_lock = threading.Lock()
_last_ms = -1
_last_random = 0


def _encode(value: int, length: int) -> str:
    """把整数编码为固定长度的大写 Crockford Base32 字符串（左侧补 `0`）。"""
    chars: list[str] = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(CROCKFORD_ALPHABET[remainder])
    return "".join(reversed(chars))


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_ulid() -> str:
    """生成 26 位单调 ULID（0.2.1：主键统一用 TEXT(26) 存 ULID）。"""
    global _last_ms, _last_random  # noqa: PLW0603 - 进程内单调计数器
    with _lock:
        now_ms = _now_ms()
        if now_ms > _last_ms:
            _last_ms = now_ms
            _last_random = secrets.randbits(_RANDOM_BITS)
        else:
            # 同一毫秒（或时钟回拨）：递增随机段，保证严格递增
            _last_random += 1
            if _last_random > _RANDOM_MAX:
                _last_ms += 1
                _last_random = secrets.randbits(_RANDOM_BITS)
            elif now_ms < _last_ms:
                now_ms = _last_ms
        timestamp_ms = max(now_ms, _last_ms)
        random_part = _last_random
    return _encode(timestamp_ms, TIMESTAMP_CHARS) + _encode(random_part, ULID_LENGTH - TIMESTAMP_CHARS)


def ulid_timestamp_ms(value: str) -> int:
    """解析 ULID 的时间戳部分（毫秒），用于测试与可观测性。

    Raises:
        ValueError: 长度非法或含非 Crockford Base32 字符。
    """
    if not is_valid_ulid(value):
        raise ValueError(f"not a valid ULID: {value!r}")
    timestamp = 0
    for char in value[:TIMESTAMP_CHARS].upper():
        timestamp = timestamp * 32 + CROCKFORD_ALPHABET.index(char)
    return timestamp


def is_valid_ulid(value: str) -> bool:
    """长度 26 且全部字符属于 Crockford Base32（大小写不敏感）。"""
    if len(value) != ULID_LENGTH:
        return False
    return all(char.upper() in CROCKFORD_ALPHABET for char in value)


def new_trace_id() -> str:
    """32 位小写十六进制（0.2.1）。"""
    return uuid4().hex


def new_span_id() -> str:
    """16 位小写十六进制（0.2.1）。"""
    return uuid4().hex[:16]


def new_request_id() -> str:
    """32 位十六进制，由 `RequestIdMiddleware` 生成（0.2.1）。"""
    return uuid4().hex
