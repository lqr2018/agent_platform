"""健康检查集成测试（7.1 测试要求：`/healthz` 200、`/readyz` 200/503；6.3）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db import session as db_session
from tests.conftest import sqlite_url


async def test_healthz_returns_ok(app_client: AsyncClient) -> None:
    response = await app_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["name"] == "agent-platform"
    assert body["app_env"] == "test"
    assert body["version"]


async def test_every_response_carries_request_id_header(app_client: AsyncClient) -> None:
    response = await app_client.get("/healthz")
    assert response.headers["X-Request-Id"]


async def test_incoming_request_id_is_reused(app_client: AsyncClient) -> None:
    """跨服务串联：入站 `X-Request-Id` 必须被复用（7.1 任务 1）。"""
    response = await app_client.get("/healthz", headers={"X-Request-Id": "trace-me-123"})
    assert response.headers["X-Request-Id"] == "trace-me-123"


async def test_readyz_ok_after_migration(app_client: AsyncClient) -> None:
    response = await app_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["database"]["ok"] is True
    assert checks["database"]["detail"].startswith("revision ")
    assert checks["vector_store"]["ok"] is True


async def test_readyz_reports_503_when_database_is_not_migrated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """未迁移的库 → 503 且给出具体原因（6.3）。"""
    from app.main import create_app

    empty_db = tmp_path / "empty.db"
    monkeypatch.setenv("DATABASE_URL", sqlite_url(empty_db))
    monkeypatch.setenv("DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("SANDBOX_ROOT", (tmp_path / "data" / "files").as_posix())
    monkeypatch.setenv("CHROMA_DIR", (tmp_path / "data" / "chroma").as_posix())
    get_settings.cache_clear()
    await db_session.dispose_engine()

    migrated_app = create_app()
    try:
        # 绕过 lifespan 的"迁移必须在 head"硬校验，直接验证探针行为
        transport = ASGITransport(app=migrated_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        database_check = next(check for check in body["checks"] if check["name"] == "database")
        assert database_check["ok"] is False
        assert "alembic_version" in database_check["detail"]
    finally:
        await db_session.dispose_engine()
        get_settings.cache_clear()


async def test_readyz_reports_503_when_chroma_dir_disappears(
    tmp_sqlite: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量库目录缺失 → 503（6.3 第 4 条）。"""
    from app.main import create_app

    missing = tmp_path / "missing-chroma"
    monkeypatch.setenv("VECTOR_STORE_KIND", "chroma")
    monkeypatch.setenv("CHROMA_DIR", missing.as_posix())
    get_settings.cache_clear()
    await db_session.dispose_engine()

    application = create_app()
    try:
        async with application.router.lifespan_context(application):
            # lifespan 已创建目录；删掉它模拟"卷没挂上"
            missing.rmdir()
            transport = ASGITransport(app=application, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                response = await client.get("/readyz")

        assert response.status_code == 503
        vector_check = next(check for check in response.json()["checks"] if check["name"] == "vector_store")
        assert vector_check["ok"] is False
        assert "directory missing" in vector_check["detail"]
    finally:
        await db_session.dispose_engine()
        get_settings.cache_clear()
