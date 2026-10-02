"""日志配置与脱敏单测（7.1 测试要求：日志脱敏过滤器；1.5.1 / 1.4 Secrets 规则 4）。"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.logging import MASK, configure_logging, get_logger, inject_trace_context, redact_mapping, redact_secrets
from app.runtime.observability import context as trace_context


def test_redact_masks_secret_keys() -> None:
    payload = {
        "api_key": "sk-live-abcdefghijklmn",
        "Authorization": "Bearer abcdefghijklmnop",
        "x_api_key": "whatever",
        "access_token": "t0ken",
        "model": "qwen-plus",
    }
    redacted = redact_mapping(payload)
    assert redacted["api_key"] == MASK
    assert redacted["Authorization"] == MASK
    assert redacted["x_api_key"] == MASK
    assert redacted["access_token"] == MASK
    assert redacted["model"] == "qwen-plus"  # 非敏感键不动


def test_redact_handles_nested_and_lists() -> None:
    payload = {"llm": {"headers": {"authorization": "Bearer zzzzzzzz"}, "keys": ["sk-abcdefghijklm"]}}
    redacted = redact_mapping(payload)
    assert redacted["llm"]["headers"]["authorization"] == MASK
    assert redacted["llm"]["keys"] == [MASK]


def test_redact_masks_secret_looking_values_even_without_key_hint() -> None:
    from_prefix = redact_mapping({"note": "use sk-abcdefghijklmnop for tests"})["note"]
    assert MASK in from_prefix
    assert "abcdefghijklmnop" not in from_prefix

    bearer = redact_mapping({"note": "Authorization: Bearer abcdefghijklmnop"})["note"]
    assert MASK in bearer
    assert "abcdefghijklmnop" not in bearer


def test_redact_does_not_mutate_input() -> None:
    payload = {"api_key": "sk-abcdefghijklmn", "nested": {"token": "abc"}}
    redact_mapping(payload)
    assert payload == {"api_key": "sk-abcdefghijklmn", "nested": {"token": "abc"}}


def test_redact_secrets_processor_returns_new_dict() -> None:
    event_dict = {"event": "llm.request", "api_key": "sk-abcdefghijklmn"}
    result = redact_secrets(None, "info", dict(event_dict))  # type: ignore[arg-type]
    assert result["api_key"] == MASK
    assert result["event"] == "llm.request"


def test_inject_trace_context_adds_ids_from_contextvars() -> None:
    trace_context.clear()
    token = trace_context.bind_request_id("req-1")
    trace_context.bind_trace("trace-1", "span-1")
    try:
        result = inject_trace_context(None, "info", {"event": "x"})  # type: ignore[arg-type]
        assert result["request_id"] == "req-1"
        assert result["trace_id"] == "trace-1"
        assert result["span_id"] == "span-1"
    finally:
        trace_context.reset_request_id(token)
        trace_context.clear()


def test_inject_trace_context_skips_unset_ids() -> None:
    trace_context.clear()
    assert "request_id" not in inject_trace_context(None, "info", {})  # type: ignore[arg-type]


@pytest.mark.parametrize("app_env", ["dev", "test", "prod"])
def test_configure_logging_is_idempotent(app_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(_env_file=None, app_env=app_env, log_level="INFO")  # type: ignore[arg-type]
    for _ in range(2):
        configure_logging(settings)
    get_logger("test").info("hello", api_key="sk-abcdefghijklmn")
    captured = capsys.readouterr().out.strip().splitlines()[-1]
    if app_env == "dev":
        assert "hello" in captured
        assert "sk-abcdefghijklmn" not in captured
    else:
        record = json.loads(captured)
        assert record["event"] == "hello"
        assert record["api_key"] == MASK
        assert record["level"] == "info"
        assert "timestamp" in record
