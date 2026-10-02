"""structlog 配置与密钥脱敏（详细设计 1.5.1 / 1.4）。

- `dev` 环境用彩色 console renderer，`test` / `prod` 用 JSON 单行（容器友好，6.5）；
- 每条日志自动注入 `request_id` / `trace_id` / `span_id`（取自 `runtime/observability/context.py` 的 ContextVar）；
- 脱敏处理器 `redact_secrets`：键名命中 `api_key|authorization|token` 的值替换为 `***`，
  字符串值命中 `sk-...` / `Bearer ...` 形态的同样替换（1.4 Secrets 规则 4）。
"""

from __future__ import annotations

import logging as stdlib_logging
import re
from collections.abc import Mapping
from typing import Any, cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from app.core.config import Settings

MASK = "***"

REDACT_KEY_PATTERN = re.compile(r"api[_-]?key|authorization|token", re.IGNORECASE)
"""1.4 规定的脱敏键名（含 `API_KEY` / `api-key` / `x-api-key` 等变体）。"""

SECRET_VALUE_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}")
"""疑似密钥的字面量（防御性：即使键名没命中，也不让密钥进日志）。"""


def _redact(key: str, value: Any) -> Any:
    if REDACT_KEY_PATTERN.search(key):
        return MASK
    if isinstance(value, str):
        return SECRET_VALUE_PATTERN.sub(MASK, value)
    if isinstance(value, Mapping):
        return {str(item_key): _redact(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, list | tuple):
        redacted = [_redact(key, item) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_mapping(mapping: Mapping[str, Any] | Any) -> Any:
    """递归脱敏任意映射（4.8.2：落 Trace 前与日志用同一套过滤器）。

    非映射入参原样返回，便于 `input` / `output` 直接透传（可能是 list / str / None）。
    """
    if isinstance(mapping, Mapping):
        return {str(key): _redact(str(key), value) for key, value in mapping.items()}
    return _redact("", mapping)


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog 处理器：脱敏后返回新的 event_dict（不改原对象）。"""
    return cast("EventDict", redact_mapping(event_dict))


def inject_trace_context(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog 处理器：注入 `request_id` / `trace_id` / `span_id`。

    延迟 import：`core` 是底层模块，模块级 import `runtime.*` 会形成反向依赖（1.2）。
    """
    from app.runtime.observability.context import current_context

    for key, value in current_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def configure_logging(settings: Settings) -> None:
    """按环境配置 structlog（可重复调用，幂等）。"""
    level = stdlib_logging.getLevelNamesMapping().get(settings.log_level.upper(), stdlib_logging.INFO)
    if settings.app_env == "dev":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer(sort_keys=False)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            inject_trace_context,
            redact_secrets,
            # 把 exc_info 渲染成文本：非 stdlib logger 不会自动处理异常信息
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """取 logger；未传 `name` 时用调用模块名。"""
    return cast("FilteringBoundLogger", structlog.get_logger(name))
