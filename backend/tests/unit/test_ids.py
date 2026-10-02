"""ID 生成单测（7.1 测试要求：ULID 单调；0.2.1 的格式约定）。"""

from __future__ import annotations

import re
import time

import pytest

from app.core.ids import (
    CROCKFORD_ALPHABET,
    ULID_LENGTH,
    is_valid_ulid,
    new_request_id,
    new_span_id,
    new_trace_id,
    new_ulid,
    ulid_timestamp_ms,
)

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def test_ulid_shape() -> None:
    value = new_ulid()
    assert len(value) == ULID_LENGTH
    assert all(char in CROCKFORD_ALPHABET for char in value)
    assert is_valid_ulid(value)


def test_ulid_is_monotonic_within_same_millisecond() -> None:
    """同一毫秒内必须严格递增（否则主键排序不稳定）。"""
    values = [new_ulid() for _ in range(5_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_ulid_timestamp_round_trip() -> None:
    before_ms = time.time_ns() // 1_000_000
    value = new_ulid()
    after_ms = time.time_ns() // 1_000_000
    assert before_ms <= ulid_timestamp_ms(value) <= after_ms


def test_ulid_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="not a valid ULID"):
        ulid_timestamp_ms("not-a-ulid")
    assert is_valid_ulid("") is False
    assert is_valid_ulid("0" * 27) is False
    # Crockford Base32 不含 I / L / O / U
    assert is_valid_ulid("I" + "0" * 25) is False


def test_trace_and_request_ids_are_32_hex() -> None:
    assert HEX32.match(new_trace_id())
    assert HEX32.match(new_request_id())
    assert len({new_trace_id() for _ in range(50)}) == 50


def test_span_id_is_16_hex() -> None:
    assert HEX16.match(new_span_id())
