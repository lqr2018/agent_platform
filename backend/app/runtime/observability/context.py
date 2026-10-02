"""Trace 上下文传播（详细设计 1.5.1 / 1.5.2）。

用 `ContextVar` 保存 `request_id` / `trace_id` / `span_id`：

- `RequestIdMiddleware`（app/main.py）在请求入口绑定 `request_id`；
- `Tracer.start_run(...)` 绑定 `trace_id` / `span_id`（1.5.2）；
- `core/logging.py` 的处理器读取本模块，实现"每条日志自动注入三个 id"（1.5.1）；
- asyncio 任务通过 `contextvars.copy_context()` 继承（Phase 1 派生 Run 任务时使用）。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)

RequestIdToken = Token[str | None]
TraceIdToken = Token[str | None]
SpanIdToken = Token[str | None]

CONTEXT_KEYS = ("request_id", "trace_id", "span_id")


def bind_request_id(request_id: str) -> RequestIdToken:
    """绑定 `request_id`，返回可用于恢复的 token。"""
    return _request_id.set(request_id)


def reset_request_id(token: RequestIdToken) -> None:
    _request_id.reset(token)


def bind_trace(trace_id: str, span_id: str | None = None) -> tuple[TraceIdToken, SpanIdToken]:
    """绑定 `trace_id`（以及可选的当前 `span_id`）。"""
    return _trace_id.set(trace_id), _span_id.set(span_id)


def reset_trace(tokens: tuple[TraceIdToken, SpanIdToken]) -> None:
    trace_token, span_token = tokens
    _trace_id.reset(trace_token)
    _span_id.reset(span_token)


def bind_span_id(span_id: str | None) -> SpanIdToken:
    return _span_id.set(span_id)


def reset_span_id(token: SpanIdToken) -> None:
    _span_id.reset(token)


def get_request_id() -> str | None:
    return _request_id.get()


def get_trace_id() -> str | None:
    return _trace_id.get()


def get_span_id() -> str | None:
    return _span_id.get()


def current_context() -> dict[str, str]:
    """当前上下文里**已绑定**的 id（未绑定则不出现该键）。"""
    values = {
        "request_id": _request_id.get(),
        "trace_id": _trace_id.get(),
        "span_id": _span_id.get(),
    }
    return {key: value for key, value in values.items() if value is not None}


def clear() -> None:
    """清空当前上下文（测试与请求收尾使用）。"""
    for var in (_request_id, _trace_id, _span_id):
        var.set(None)
