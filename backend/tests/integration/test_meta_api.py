"""`/api/v1/meta` 集成测试（3.2.1 / 3.1 统一响应封装）。"""

from __future__ import annotations

from httpx import AsyncClient

BACKLOG_FEATURES = ("mcp", "memory", "eval_platform")


async def test_meta_uses_success_envelope(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert body["data"]["name"] == "agent-platform"
    assert body["data"]["app_env"] == "test"
    assert body["data"]["version"]


async def test_meta_request_id_matches_header(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/meta")
    body = response.json()
    assert body["meta"]["request_id"] == response.headers["X-Request-Id"]
    assert body["meta"]["trace_id"] is None  # Phase 0 无 Run，故无 trace


async def test_meta_features_are_false_for_backlog(app_client: AsyncClient) -> None:
    """前端据 features 隐藏 Backlog 菜单（5.2 / SD-14②）。"""
    body = (await app_client.get("/api/v1/meta")).json()
    for feature in BACKLOG_FEATURES:
        assert body["data"]["features"][feature] is False


async def test_meta_is_registered_under_api_v1(app_client: AsyncClient) -> None:
    assert (await app_client.get("/meta")).status_code == 404
    assert (await app_client.get("/api/v1/meta")).status_code == 200


async def test_openapi_contains_phase0_and_phase1_endpoints(app_client: AsyncClient) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])

    assert {"/healthz", "/readyz", "/api/v1/meta"} <= paths
    # Phase 1（7.2 接口变更）：3.2.2 + 3.2.4 + 3.2.9 的 traces / spans
    assert {
        "/api/v1/model-providers",
        "/api/v1/model-providers/{provider_id}",
        "/api/v1/model-providers/{provider_id}/test",
        "/api/v1/agents",
        "/api/v1/agents/{agent_id}",
        "/api/v1/agents/{agent_id}/clone",
        "/api/v1/conversations",
        "/api/v1/conversations/{conversation_id}/messages",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}/cancel",
        "/api/v1/traces",
        "/api/v1/traces/{trace_id}",
        "/api/v1/spans/{span_id}",
    } <= paths


async def test_backlog_endpoints_are_not_registered(app_client: AsyncClient) -> None:
    """SD-14②：Backlog 能力不注册路由（也不返回空实现）。"""
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    assert not {path for path in paths if "approval" in path or "mcp" in path or "eval" in path or "memor" in path}
