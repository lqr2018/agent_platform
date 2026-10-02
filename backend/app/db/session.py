"""async engine / sessionmaker / 迁移与连通性探针（详细设计 7.1 任务 3–4、6.3）。

`app/api/deps.py` 从这里转出 `get_session` 依赖；`app/main.py` 的 lifespan 用
`verify_migrations_at_head()` 做启动自检第 1 条（不等则直接退出，6.3）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = BACKEND_ROOT / "alembic.ini"

_engine: AsyncEngine | None = None
_engine_url: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """进程内单例 engine（懒创建）。

    `DATABASE_URL` 变化时（测试换库、或运维改配置）自动重建，避免用到上一个库的连接池。
    """
    global _engine, _engine_url  # noqa: PLW0603 - 模块级单例
    config = settings or get_settings()
    if _engine is None or _engine_url != config.database_url:
        _engine = create_async_engine(config.database_url, echo=False, future=True)
        _engine_url = config.database_url
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """进程内单例 sessionmaker；**绑定到当前 engine**（engine 因 URL 变化重建时同步重建）。"""
    global _session_factory  # noqa: PLW0603 - 模块级单例
    engine = get_engine()
    if _session_factory is None or _session_factory.kw["bind"] is not engine:
        _session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个 session；异常时回滚（api/deps.py 转出）。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """关闭连接池并清空单例（lifespan 结束时调用；测试用于切换 `DATABASE_URL`）。"""
    global _engine, _engine_url, _session_factory  # noqa: PLW0603 - 模块级单例
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_url = None
    _session_factory = None


# ---- Alembic / 启动自检（6.3） ----
def alembic_config() -> AlembicConfig:
    """构造 Alembic 配置（脚本目录固定为 `backend/alembic`）。"""
    config = AlembicConfig(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


def head_revision() -> str:
    """当前迁移脚本的 head 版本号。"""
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    if head is None:  # pragma: no cover - 仓库里至少有一个迁移
        raise RuntimeError("alembic script directory has no head revision")
    return head


async def current_revision() -> str | None:
    """数据库里的 `alembic_version.version_num`；表不存在（未迁移）返回 `None`。"""
    async with get_engine().connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            return None
        return result.scalar_one_or_none()


async def check_database() -> tuple[bool, str]:
    """DB 就绪检查：可连接 + **可写** + 迁移已到 head（6.3 `/readyz`）。"""
    try:
        async with get_engine().connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
            try:
                # BEGIN IMMEDIATE 会立刻抢占 SQLite 写锁：拿到锁即证明库可写，随后立即回滚
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
                await conn.rollback()
            except SQLAlchemyError as exc:
                return False, f"database is not writable: {type(exc).__name__}"
    except SQLAlchemyError as exc:
        return False, f"database is unreachable: {type(exc).__name__}: {exc}"

    expected = head_revision()
    found = await current_revision()
    if found is None:
        return False, "database is not migrated (alembic_version not found); run `alembic upgrade head`"
    if found != expected:
        return False, f"schema revision mismatch: db={found} head={expected}"
    return True, f"revision {found}"


async def verify_migrations_at_head() -> None:
    """lifespan 启动自检第 1 条：迁移版本必须等于 head，否则直接退出（6.3）。"""
    expected = head_revision()
    found = await current_revision()
    if found != expected:
        raise RuntimeError(
            f"Database schema is not at head (db={found}, head={expected}). Run `alembic upgrade head` first."
        )
