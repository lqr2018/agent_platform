"""async engine / sessionmaker / 迁移与连通性探针（详细设计 7.1 任务 3–4、6.3）。

`app/api/deps.py` 从这里转出 `get_session` 依赖；`app/main.py` 的 lifespan 用
`verify_migrations_at_head()` 做启动自检第 1 条（不等则直接退出，6.3）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = BACKEND_ROOT / "alembic.ini"

SQLITE_BUSY_TIMEOUT_SECONDS = 10.0
"""SQLite 写锁等待上限（秒）。

默认的 5s 对"后台 Run 落 `node_runs` + 前端轮询 + `/readyz` 的 `BEGIN IMMEDIATE`"三者并存时偏紧：
一旦等到超时就抛 `OperationalError: database is locked`，而**后台任务里的写失败是静默的**（4.8.2：
观测落库不影响主流程）—— 表现为"节点行状态偶尔停在 running"这类难查的现象。10s 覆盖正常的
写事务排队，同时把真正的死锁暴露时间控制在可接受范围内。
"""

SQLITE_PRAGMAS: tuple[str, ...] = (
    # WAL：读不被写阻塞（详情/节点表轮询不再和后台落库互相排队），写与写仍是串行的
    "PRAGMA journal_mode=WAL",
    # WAL 下的常规取舍：崩溃时不丢已提交数据，比默认的 FULL 少一次 fsync/事务
    "PRAGMA synchronous=NORMAL",
)
"""每种连接建立时执行的 PRAGMA（只有 SQLite 会用到）。"""


def _sqlite_url(database_url: str) -> bool:
    """是否 SQLite URL（非 SQLite 或 URL 非法一律返回 `False`，由建 engine 时再报错）。"""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return False
    return url.drivername.startswith("sqlite")


def _connect_args(database_url: str) -> dict[str, Any]:
    """`aiosqlite` 的 `timeout`（秒）在**驱动层**等锁，比在 SQLAlchemy 层重试更省事。

    它与 `PRAGMA busy_timeout` 是同一件事的两种写法（驱动用自己的默认值，PRAGMA 覆盖它），
    两处都设是为了"无论谁先生效都一致"。
    """
    return {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS} if _sqlite_url(database_url) else {}


def _install_sqlite_pragmas(engine: AsyncEngine, database_url: str) -> None:
    """给每个**新连接**挂上 PRAGMA（`journal_mode` / `synchronous` / `busy_timeout`）。

    用 `connect` 事件而不是 `connect_args`：PRAGMA 在连接建立后立刻执行，连接池里后续
    复用的连接都带着这些设置；`:memory:` 库上 `journal_mode=WAL` 只会返回 `memory`，不报错。
    """
    if not _sqlite_url(database_url):
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma in (*SQLITE_PRAGMAS, f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"):
                cursor.execute(pragma)
        finally:
            cursor.close()


_engine: AsyncEngine | None = None
_engine_url: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def ensure_sqlite_directory(database_url: str) -> None:
    """确保文件型 SQLite 的**父目录**存在（2.14 / 6.3）。

    为什么需要：`DATABASE_URL` 默认指向 `./data/app.db`，而 `data/` 在 `.gitignore` 里——
    干净检出（CI、新克隆的仓库、挂载到空卷的容器）时目录并不存在，SQLite 会直接抛
    `unable to open database file`，迁移和启动都会失败。这里在建 engine 之前补齐目录，
    让"配置里写了哪个路径就一定跑得起来"。

    只处理文件型 SQLite：`:memory:`、URI 形式（`file:`）与非 SQLite（SD-8 不实现 PG）一律跳过。
    """

    try:
        url = make_url(database_url)
    except ArgumentError:  # pragma: no cover - URL 由 Settings 提供，非法值随后会在建 engine 时报错
        return
    if not url.drivername.startswith("sqlite"):
        return
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return
    # SQLite 的**相对路径按进程 cwd 解析**，所以这里也用同一套规则（不 resolve，避免改变语义）
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """进程内单例 engine（懒创建）。

    `DATABASE_URL` 变化时（测试换库、或运维改配置）自动重建，避免用到上一个库的连接池。
    """
    global _engine, _engine_url  # noqa: PLW0603 - 模块级单例
    config = settings or get_settings()
    if _engine is None or _engine_url != config.database_url:
        ensure_sqlite_directory(config.database_url)
        _engine = create_async_engine(
            config.database_url,
            echo=False,
            future=True,
            connect_args=_connect_args(config.database_url),
        )
        _install_sqlite_pragmas(_engine, config.database_url)
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
