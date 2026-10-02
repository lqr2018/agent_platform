"""SQLite 数据目录自愈的单元测试（2.14 / 6.3）。

背景（一次真实的 CI 回归）：`DATABASE_URL` 默认是 `./data/app.db`，而 `backend/data/`
在 `.gitignore` 里——**干净检出时目录并不存在**，SQLite 会立刻抛
`unable to open database file`，于是 CI 的 `alembic upgrade head` 直接失败
（本地/Docker 因为目录早已存在而看不出来，Docker 镜像里还有 `RUN mkdir -p /app/data`）。
修复方式：建 engine 之前统一走 `ensure_sqlite_directory()` 补齐父目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.db import session as db_session
from app.db.session import ensure_sqlite_directory


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    """多级目录也一次建出来；只建目录，不创建数据库文件本身。"""
    db_path = tmp_path / "cold" / "nested" / "app.db"

    ensure_sqlite_directory(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    assert db_path.parent.is_dir()
    assert not db_path.exists()


def test_keeps_existing_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "app.db"
    db_path.parent.mkdir(parents=True)

    ensure_sqlite_directory(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    assert db_path.parent.is_dir()


def test_relative_url_is_resolved_against_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认配置就是相对路径 `./data/app.db`，目录要建在 cwd 下（与 SQLite 的解析规则一致）。"""
    monkeypatch.chdir(tmp_path)

    ensure_sqlite_directory("sqlite+aiosqlite:///./data/app.db")

    assert (tmp_path / "data").is_dir()


def test_memory_sqlite_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    ensure_sqlite_directory("sqlite+aiosqlite:///:memory:")

    assert list(tmp_path.iterdir()) == []


def test_sqlite_file_uri_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`file:` 形式是 SQLite URI（2.11 的共享内存库等），不做目录预处理。"""
    monkeypatch.chdir(tmp_path)

    ensure_sqlite_directory("sqlite+aiosqlite:///file:memdb1?mode=memory&cache=shared")

    assert list(tmp_path.iterdir()) == []


def test_non_sqlite_driver_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SD-8：只保证 SQLite；其它驱动的 `database` 不是路径，绝不能在 cwd 建同名目录。"""
    monkeypatch.chdir(tmp_path)

    ensure_sqlite_directory("postgresql+asyncpg://user:pw@localhost/appdb")

    assert list(tmp_path.iterdir()) == []


def test_invalid_url_is_ignored() -> None:
    """URL 非法时不在这里报错（随后 `create_async_engine` 会给出更清晰的错误）。"""
    ensure_sqlite_directory("not a url")


@pytest.mark.asyncio
async def test_get_engine_creates_directory_for_cold_path(tmp_path: Path) -> None:
    """`get_engine()` 是应用的唯一建 engine 入口，冷路径也必须能自愈（6.3 启动自检的前置条件）。"""
    db_path = tmp_path / "cold" / "app.db"
    await db_session.dispose_engine()
    settings = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}")
    try:
        engine = db_session.get_engine(settings)
        assert db_path.parent.is_dir()
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    finally:
        await db_session.dispose_engine()
