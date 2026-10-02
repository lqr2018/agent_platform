"""统一响应封装单测（3.1 / 1.6 / 0.2.1 时间格式）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pydantic import BaseModel

from app.runtime.observability import context as trace_context
from app.schemas.common import ApiResponse, ErrorResponse, ResponseMeta, UtcDatetime, format_utc


class Payload(BaseModel):
    at: UtcDatetime


def test_format_utc_marks_naive_values_as_utc() -> None:
    naive = datetime(2026, 9, 29, 12, 0, 0, 123_000)  # noqa: DTZ001 - 有意构造 naive 值验证兜底
    assert format_utc(naive) == "2026-09-29T12:00:00.123Z"


def test_format_utc_converts_aware_values_to_utc() -> None:
    beijing = timezone(timedelta(hours=8))
    assert format_utc(datetime(2026, 9, 29, 20, 0, 0, 0, tzinfo=beijing)) == "2026-09-29T12:00:00.000Z"


def test_utc_datetime_serializes_with_z_suffix() -> None:
    model = Payload(at=datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC))
    assert model.model_dump(mode="json")["at"] == "2026-09-29T12:00:00.000Z"


def test_api_response_reads_request_id_from_context() -> None:
    trace_context.clear()
    token = trace_context.bind_request_id("req-abc")
    trace_context.bind_trace("trace-abc", "span-abc")
    try:
        response = ApiResponse[dict].of({"hello": "world"})
        payload = response.model_dump(mode="json")
        assert payload["data"] == {"hello": "world"}
        assert payload["meta"]["request_id"] == "req-abc"
        assert payload["meta"]["trace_id"] == "trace-abc"
        assert payload["meta"]["page"] is None
    finally:
        trace_context.reset_request_id(token)
        trace_context.clear()


def test_api_response_supports_pagination_meta() -> None:
    trace_context.clear()
    response = ApiResponse[list].of([1, 2], page=2, page_size=1, total=3)
    assert response.meta.page == 2
    assert response.meta.page_size == 1
    assert response.meta.total == 3


def test_error_response_shape() -> None:
    response = ErrorResponse.of("NOT_FOUND", "Resource not found", details={"id": "x"})
    payload = response.model_dump(mode="json")
    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["error"]["details"] == {"id": "x"}
    assert "meta" in payload


def test_response_meta_falls_back_to_request_state() -> None:
    class _State:
        request_id = "req-from-state"

    class _FakeRequest:
        state = _State()

    trace_context.clear()
    meta = ResponseMeta.from_context(request=_FakeRequest())  # type: ignore[arg-type]
    assert meta.request_id == "req-from-state"
