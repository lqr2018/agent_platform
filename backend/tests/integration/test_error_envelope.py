"""错误响应契约集成测试（7.1 测试要求：任意异常响应体符合 1.6，且 `meta.request_id` 与日志一致）。

测试用的探针路由在测试内动态注册（`app.add_api_route`），**不进入生产路由表**。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.errors import ConflictError, InternalError, NotFoundError, ValidationError
from app.main import REQUEST_ID_HEADER


async def _raise_not_found() -> None:
    raise NotFoundError("Agent not found", details={"agent_id": "01H"})


async def _raise_conflict() -> None:
    raise ConflictError("Agent is in use")


async def _raise_validation() -> None:
    raise ValidationError(details={"errors": [{"loc": ["body", "name"], "msg": "required"}]})


async def _raise_unexpected() -> None:
    raise RuntimeError("internal detail that must not leak")


async def _echo_number(value: int) -> dict[str, int]:
    return {"value": value}


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    app.add_api_route("/_test/not-found", _raise_not_found, methods=["GET"])
    app.add_api_route("/_test/conflict", _raise_conflict, methods=["GET"])
    app.add_api_route("/_test/validation", _raise_validation, methods=["GET"])
    app.add_api_route("/_test/unexpected", _raise_unexpected, methods=["GET"])
    app.add_api_route("/_test/echo", _echo_number, methods=["GET"])
    return app


@pytest.fixture
async def probe_client(probe_app: FastAPI):
    from httpx import ASGITransport

    try:
        async with probe_app.router.lifespan_context(probe_app):
            transport = ASGITransport(app=probe_app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield client
    finally:
        from app.db import session as db_session

        await db_session.dispose_engine()


async def test_app_error_maps_code_and_status(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/_test/not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Agent not found"
    assert body["error"]["details"] == {"agent_id": "01H"}
    assert body["meta"]["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_conflict_maps_to_409(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/_test/conflict")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_request_validation_error_has_field_level_details(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/_test/echo", params={"value": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["errors"]
    assert body["error"]["details"]["errors"][0]["loc"] == ["query", "value"]


async def test_missing_required_query_param_is_422(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/_test/echo")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unexpected_exception_returns_internal_error_without_stack(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/_test/unexpected")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": InternalError.message, "details": {}}
    raw = response.text
    assert "internal detail that must not leak" not in raw
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw
    assert body["meta"]["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_unknown_path_returns_not_found_envelope(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/definitely-not-here")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_method_not_allowed_is_enveloped(probe_client: AsyncClient) -> None:
    response = await probe_client.post("/healthz")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "NOT_IMPLEMENTED"


async def test_unhandled_exception_request_id_matches_log(
    probe_client: AsyncClient,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """DoD 第 4 条：`meta.request_id` 与日志中的一致。"""
    capfd.readouterr()  # 清空既有输出
    response = await probe_client.get("/_test/unexpected")
    request_id = response.json()["meta"]["request_id"]

    records = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")]
    matching = [record for record in records if record.get("request_id") == request_id]
    assert matching, f"未在日志中找到 request_id={request_id} 的记录：{records}"

    stack_records = [record for record in matching if record["event"] == "unhandled_exception"]
    assert stack_records, f"未找到 unhandled_exception 日志：{matching}"
    # 堆栈只进日志（1.6）：日志里有异常文本，响应体里没有
    assert "RuntimeError" in json.dumps(stack_records[0], ensure_ascii=False)


async def test_access_log_records_status_and_request_id(
    probe_client: AsyncClient,
    capfd: pytest.CaptureFixture[str],
) -> None:
    capfd.readouterr()
    response = await probe_client.get("/healthz")
    request_id = response.headers[REQUEST_ID_HEADER]

    records = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")]
    access = next(record for record in records if record["event"] == "http.request")
    assert access["request_id"] == request_id
    assert access["status"] == 200
    assert access["path"] == "/healthz"
    assert access["duration_ms"] >= 0
