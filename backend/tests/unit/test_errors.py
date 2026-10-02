"""`AppError` 体系单测（7.1 测试要求：AppError → HTTP 映射，1.6 / 附录 A）。"""

from __future__ import annotations

import pytest

from app.core.errors import (
    HTTP_STATUS_TO_CODE,
    AppError,
    ConfigInvalidError,
    ConflictError,
    ErrorCode,
    FeatureNotImplementedError,
    InternalError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)

PHASE_0_MAPPING = [
    (ValidationError, ErrorCode.VALIDATION_ERROR, 422),
    (NotFoundError, ErrorCode.NOT_FOUND, 404),
    (ConflictError, ErrorCode.CONFLICT, 409),
    (RateLimitedError, ErrorCode.RATE_LIMITED, 429),
    (FeatureNotImplementedError, ErrorCode.NOT_IMPLEMENTED, 501),
    (InternalError, ErrorCode.INTERNAL_ERROR, 500),
    (ConfigInvalidError, ErrorCode.CONFIG_INVALID, 500),
]


@pytest.mark.parametrize(("error_cls", "code", "http_status"), PHASE_0_MAPPING)
def test_error_code_and_http_status(error_cls: type[AppError], code: ErrorCode, http_status: int) -> None:
    error = error_cls()
    assert error.code is code
    assert str(error.code) == code.value
    assert error.http_status == http_status


@pytest.mark.parametrize(("error_cls", "code", "http_status"), PHASE_0_MAPPING)
def test_to_body_shape(error_cls: type[AppError], code: ErrorCode, http_status: int) -> None:
    error = error_cls("custom message", details={"field": "value"})
    body = error.to_body()
    assert body == {"code": code.value, "message": "custom message", "details": {"field": "value"}}
    assert error.http_status == http_status


def test_default_message_used_when_omitted() -> None:
    assert ValidationError().message == ValidationError.message
    assert str(NotFoundError()) == NotFoundError.message


def test_details_are_not_shared_between_instances() -> None:
    first = AppError()
    second = AppError()
    first.details["x"] = 1
    assert second.details == {}


def test_status_to_code_mapping_covers_phase0_codes() -> None:
    assert HTTP_STATUS_TO_CODE[404] is ErrorCode.NOT_FOUND
    assert HTTP_STATUS_TO_CODE[409] is ErrorCode.CONFLICT
    assert HTTP_STATUS_TO_CODE[422] is ErrorCode.VALIDATION_ERROR
    assert HTTP_STATUS_TO_CODE[429] is ErrorCode.RATE_LIMITED
    assert HTTP_STATUS_TO_CODE[501] is ErrorCode.NOT_IMPLEMENTED
    assert HTTP_STATUS_TO_CODE[500] is ErrorCode.INTERNAL_ERROR
