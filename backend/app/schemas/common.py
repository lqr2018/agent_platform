"""通用 DTO 与统一响应封装（详细设计 3.1 / 1.6 / 0.2.1）。

成功：`{ "data": …, "meta": { request_id, trace_id, page, page_size, total } }`
失败：`{ "error": { code, message, details }, "meta": { request_id, trace_id } }`

根路径探针（`/healthz`、`/readyz`）不走本封装（3.2.1：它们在根路径且是基础设施探针）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel, Field, PlainSerializer

from app.runtime.observability import context as trace_context


def format_utc(value: datetime) -> str:
    """UTC 时间 → `2026-09-29T12:00:00.123Z`（3.1 时间格式）。naive 值按 UTC 解释。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(format_utc, return_type=str, when_used="json")]
"""所有对外 DTO 的时间字段类型（JSON 序列化时自动补 `Z`）。"""

T = TypeVar("T")


class ResponseMeta(BaseModel):
    """`meta` 字段；`page*` / `total` 只在列表接口出现（3.1）。

    `next_cursor` 是游标分页接口（`messages` / `traces`）的补充字段：
    为 `None` 表示没有更多数据；请求时把它原样回传 `?cursor=`（**不透明字符串**）。"""

    request_id: str = ""
    trace_id: str | None = None
    page: int | None = None
    page_size: int | None = None
    total: int | None = None
    next_cursor: str | None = None

    @classmethod
    def from_context(
        cls,
        *,
        request: Request | None = None,
        page: int | None = None,
        page_size: int | None = None,
        total: int | None = None,
        next_cursor: str | None = None,
    ) -> ResponseMeta:
        """从 ContextVar 取 `request_id` / `trace_id`（1.5.1）。

        `request` 用于兜底：未捕获异常由 `ServerErrorMiddleware` 在中间件**之外**处理，
        此时 ContextVar 已被还原，改从 `request.state.request_id` 读取（中间件写入）。
        """
        request_id = trace_context.get_request_id()
        if not request_id and request is not None:
            request_id = getattr(request.state, "request_id", "")
        return cls(
            request_id=request_id or "",
            trace_id=trace_context.get_trace_id(),
            page=page,
            page_size=page_size,
            total=total,
            next_cursor=next_cursor,
        )


class ApiResponse(BaseModel, Generic[T]):
    """成功响应（3.1）。"""

    data: T
    meta: ResponseMeta

    @classmethod
    def of(
        cls,
        data: T,
        *,
        page: int | None = None,
        page_size: int | None = None,
        total: int | None = None,
        next_cursor: str | None = None,
    ) -> ApiResponse[T]:
        return cls(
            data=data,
            meta=ResponseMeta.from_context(page=page, page_size=page_size, total=total, next_cursor=next_cursor),
        )


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """错误响应（1.6）。`details` 只放安全、可展示的信息。"""

    error: ErrorBody
    meta: ResponseMeta

    @classmethod
    def of(
        cls,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> ErrorResponse:
        return cls(
            error=ErrorBody(code=code, message=message, details=dict(details or {})),
            meta=ResponseMeta.from_context(request=request),
        )
