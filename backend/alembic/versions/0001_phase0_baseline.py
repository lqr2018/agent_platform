"""phase0 baseline（无表，仅建立迁移基线）

Revision ID: 0001_phase0_baseline
Revises:
Create Date: 2026-10-01

Phase 0 的数据模型变更：**无表**（详细设计 7.1），本迁移只建立基线，
让 `alembic upgrade head` / `downgrade base` 往返可跑（DoD 第 3 条）。

SQLite + `batch_alter_table` 的兼容性验证（7.1 风险对策）放在
`tests/integration/test_sqlite_batch_mode.py`：那里用临时库建一张含 CHECK 约束的表，
再走 batch 模式做 ADD COLUMN / DROP COLUMN，避免把"验证用的表"留在业务 schema 里。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_phase0_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Phase 0：无表。"""


def downgrade() -> None:
    """回到空库。"""
