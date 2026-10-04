"""迁移集成测试（7.1 / 7.2 测试要求 / 9.3 门禁：`alembic upgrade` / `downgrade` 往返成功）。

按 2.14：测试库必须走 `alembic upgrade head`（不使用 `create_all()`），保证迁移脚本本身被测到。
`HEAD_REVISION` 每个 Phase 更新一次（与 `alembic/versions/` 的 head 一致）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.db.base import Base
from tests.conftest import alembic_config_for, sqlite_url

PHASE0_REVISION = "0001_phase0_baseline"
PHASE1_REVISION = "0002_phase1_core_tables"
HEAD_REVISION = "0003_phase2_tool_tables"

PHASE1_TABLES = {
    "model_providers",
    "agents",
    "agent_prompt_versions",
    "conversations",
    "messages",
    "runs",
    "traces",
    "spans",
}
"""7.2 交付物的 8 张表（Phase 1）。"""

PHASE2_TABLES = PHASE1_TABLES | {"tools", "tool_invocations"}
"""Phase 2 追加 `tools` / `tool_invocations`（2.5；`approvals` 属 Backlog，SD-17）。"""

TOOL_SEED_NAMES = ["calculator", "file_read", "file_write", "python_execute", "web_search"]
"""4.2.2 的 5 个内置工具（迁移 `0003` 的数据迁移写入）。"""


def _sync_sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def test_upgrade_downgrade_upgrade_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "round_trip.db"
    config = alembic_config_for(sqlite_url(db_path))

    command.upgrade(config, "head")
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD_REVISION

    command.downgrade(config, "base")
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        # downgrade 后 alembic 只清空版本行，版本表本身保留
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert rows == []

    command.upgrade(config, "head")
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD_REVISION


def test_phase0_revision_creates_no_business_tables(tmp_path: Path) -> None:
    """7.1：只升到 Phase 0 时，数据模型变更 = 无表（仅 `alembic_version`）。"""
    db_path = tmp_path / "phase0.db"
    command.upgrade(alembic_config_for(sqlite_url(db_path)), PHASE0_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert tables == {"alembic_version"}


def test_phase1_migration_matches_models(tmp_path: Path) -> None:
    """7.2：升到 Phase 1 时只出现那 8 张表（Phase 2 的 `tools` 还不该存在）。"""
    db_path = tmp_path / "phase1.db"
    command.upgrade(alembic_config_for(sqlite_url(db_path)), PHASE1_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())

    assert tables == PHASE1_TABLES | {"alembic_version"}


def test_phase2_migration_matches_models(tmp_path: Path) -> None:
    """7.2：head 的迁移必须与 `Base.metadata`（ORM 模型）完全一致（2.14）。"""
    db_path = tmp_path / "phase2.db"
    command.upgrade(alembic_config_for(sqlite_url(db_path)), HEAD_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())

    assert tables == PHASE2_TABLES | {"alembic_version"}
    assert set(Base.metadata.tables) == PHASE2_TABLES


def test_phase2_migration_seeds_builtin_tools(tmp_path: Path) -> None:
    """4.2.2：迁移 `0003` 的数据迁移必须把 5 个内置工具写入 `tools`。

    种子内容是否与代码定义一致由 `tests/unit/test_tool_registry.py` 负责（逐字段比对
    `registry.seed_rows()`）；这里只锁"迁移后即可用"这一交付物。
    """
    db_path = tmp_path / "phase2_seed.db"
    command.upgrade(alembic_config_for(sqlite_url(db_path)), HEAD_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        rows = conn.execute(text("SELECT name, tool_type, status, is_system FROM tools ORDER BY name")).fetchall()

    assert [row[0] for row in rows] == TOOL_SEED_NAMES
    assert {row[1] for row in rows} == {"builtin"}
    assert {row[2] for row in rows} == {"enabled"}
    assert all(row[3] for row in rows)


def test_downgrade_only_drops_phase2_tables(tmp_path: Path) -> None:
    """`0003` 的 downgrade 必须只回退 Phase 2（Phase 1 的表与数据保留）。"""
    db_path = tmp_path / "phase2_downgrade.db"
    config = alembic_config_for(sqlite_url(db_path))
    command.upgrade(config, "head")
    command.downgrade(config, PHASE1_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())

    assert tables == PHASE1_TABLES | {"alembic_version"}


def test_upgrade_creates_missing_database_directory(tmp_path: Path) -> None:
    """干净检出场景（CI 的真实失败点）：父目录不存在时 `alembic upgrade head` 也必须成功。

    `backend/data/` 不在版本库里，CI 从零 clone 后目录不存在，早期实现会直接抛
    `unable to open database file`；现在由 `app/db/session.py::ensure_sqlite_directory()`
    在 `alembic/env.py` 建 engine 之前补齐目录（2.14 / 6.3）。
    """

    db_path = tmp_path / "not_created_yet" / "data" / "ci.db"
    assert not db_path.parent.exists()

    command.upgrade(alembic_config_for(sqlite_url(db_path)), "head")

    assert db_path.is_file()
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert tables == PHASE2_TABLES | {"alembic_version"}


def test_head_revision_matches_script_directory() -> None:
    from app.db.session import head_revision

    assert head_revision() == HEAD_REVISION
