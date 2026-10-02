"""pytest 全局夹具（详细设计 7.1 任务 7、9.1、2.14）。

- 测试用**临时 SQLite 文件**并执行 `alembic upgrade head`，**不调用 `create_all()`**（2.14）；
- `app_client`：`httpx.ASGITransport`（不启真实服务，9.1）并手动驱动 lifespan，
  以便覆盖 6.3 的启动自检；
- 所有测试禁止真实外网调用（9.2）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.core.config import get_settings
from app.db import session as db_session

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"


def alembic_config_for(database_url: str) -> AlembicConfig:
    """给指定 URL 构造 Alembic 配置（避免测试互相污染 ini）。"""
    config = db_session.alembic_config()
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


@pytest.fixture
def tmp_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """临时库 + 迁移到 head；同时把数据目录指向 tmp（避免污染 `backend/data/`）。"""
    db_path = tmp_path / "app.db"
    url = sqlite_url(db_path)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", data_dir.as_posix())
    monkeypatch.setenv("SANDBOX_ROOT", (data_dir / "files").as_posix())
    monkeypatch.setenv("CHROMA_DIR", (data_dir / "chroma").as_posix())
    get_settings.cache_clear()
    command.upgrade(alembic_config_for(url), "head")
    yield db_path
    get_settings.cache_clear()


@pytest.fixture
def app(tmp_sqlite: Path) -> FastAPI:
    """按当前环境变量构造应用（`create_app` 会重新读取 Settings）。"""
    from app.main import create_app

    get_settings.cache_clear()
    return create_app()


@pytest_asyncio.fixture
async def session(tmp_sqlite: Path) -> AsyncIterator[AsyncSession]:
    """直连临时库的 session（断言落库结果用）。

    退出时必须 `dispose_engine()`：否则下一个测试换 `DATABASE_URL` 后，
    缓存里的 sessionmaker 仍绑定旧 engine，测试之间会互相污染。
    """
    from app.db.session import get_sessionmaker

    try:
        async with get_sessionmaker()() as db:
            yield db
    finally:
        await db_session.dispose_engine()
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def app_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """已跑 lifespan 的测试客户端；退出时归还连接池。

    `raise_app_exceptions=False`：Starlette 的 `ServerErrorMiddleware` 在返回 500 草稿后
    仍会 re-raise，这里要的是"客户端看到的响应"，而不是异常本身。
    """
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield client
    finally:
        await db_session.dispose_engine()
        get_settings.cache_clear()
