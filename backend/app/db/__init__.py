"""数据库层：`base.py`（ORM 基类与公共类型）+ `session.py`（async engine / sessionmaker）。

`db/models/` 与第 2 章的表一一对应，随 Phase 增长（Phase 0 无表，只有 `alembic_version`）。
"""

from __future__ import annotations
