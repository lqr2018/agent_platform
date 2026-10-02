"""SQLite + Alembic batch 模式验证（7.1 风险对策）。

> SQLite + async（`aiosqlite`）与 Alembic batch 模式的兼容性是本阶段最大坑 →
> 在 Phase 0 就用一个含 CHECK 约束的示例迁移验证 `batch_alter_table`，而不是等到 Phase 2。

验证方式：临时库 → 建一张带命名 CHECK 约束的表 → 用 `batch_alter_table` 加列 / 删列 →
断言约束**仍然被数据库强制执行**（语义保证），且列变更生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

CHECK_EXPRESSION = "kind IN ('alpha', 'beta')"


def _create_probe_table(op: Operations) -> None:
    op.create_table(
        "batch_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.CheckConstraint(CHECK_EXPRESSION, name="ck_batch_probe_kind"),
    )


def test_batch_alter_table_keeps_check_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "batch.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    with engine.begin() as conn:
        migration_context = MigrationContext.configure(conn, opts={"render_as_batch": True})
        with Operations.context(migration_context) as op:
            _create_probe_table(op)
            with op.batch_alter_table("batch_probe") as batch_op:
                batch_op.add_column(sa.Column("note", sa.String(32), nullable=True))
            with op.batch_alter_table("batch_probe") as batch_op:
                batch_op.drop_column("note")

    with engine.connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("batch_probe")}
        create_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='batch_probe'")
        ).scalar_one()

    assert columns == {"id", "kind"}  # 加列 + 删列都生效
    assert "ck_batch_probe_kind" in create_sql  # 命名约束在 batch 重建后保留


def test_check_constraint_is_still_enforced_after_batch_rebuild(tmp_path: Path) -> None:
    """batch 模式的真正风险是"重建表后丢掉约束"，这里用行为验证（不只是看 DDL）。"""
    db_path = tmp_path / "enforce.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    with engine.begin() as conn:
        migration_context = MigrationContext.configure(conn, opts={"render_as_batch": True})
        with Operations.context(migration_context) as op:
            _create_probe_table(op)
            with op.batch_alter_table("batch_probe") as batch_op:
                batch_op.add_column(sa.Column("note", sa.String(32), nullable=True))

    with engine.connect() as conn:
        conn.execute(text("INSERT INTO batch_probe (kind, note) VALUES ('alpha', 'ok')"))
        conn.commit()

    def _insert_invalid_kind() -> None:
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO batch_probe (kind, note) VALUES ('gamma', 'bad')"))
            conn.commit()

    with pytest.raises(IntegrityError):
        _insert_invalid_kind()
