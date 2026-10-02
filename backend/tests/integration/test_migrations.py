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
HEAD_REVISION = "0002_phase1_core_tables"

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
"""7.2 交付物的 8 张表。"""


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
    """7.2：Phase 1 的迁移必须与 `Base.metadata`（ORM 模型）完全一致（2.14）。"""
    db_path = tmp_path / "phase1.db"
    command.upgrade(alembic_config_for(sqlite_url(db_path)), HEAD_REVISION)
    with create_engine(_sync_sqlite_url(db_path)).connect() as conn:
        tables = set(inspect(conn).get_table_names())

    assert tables == PHASE1_TABLES | {"alembic_version"}
    assert set(Base.metadata.tables) == PHASE1_TABLES


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
    assert tables == PHASE1_TABLES | {"alembic_version"}


def test_head_revision_matches_script_directory() -> None:
    from app.db.session import head_revision

    assert head_revision() == HEAD_REVISION
