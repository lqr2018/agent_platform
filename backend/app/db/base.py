"""ORM 基类与公共类型（详细设计 2.2 / 0.2.1 / 2.14）。

- `Base.metadata` **只作为 alembic autogenerate 的输入**；任何环境都不调用 `create_all()`（2.14）；
- 主键统一 `UUIDStr`（TEXT(26) 存 ULID，0.2.1）；
- JSON 列用 SQLAlchemy `JSON`（SQLite 原生支持）：
  如将来接 PostgreSQL，在 `env.py` 按方言换成
  `JSON().with_variant(postgresql.JSONB(), "postgresql")`（2.14 已在文档中记录，Phase 0 不实现）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import new_ulid

ULID_LENGTH = 26


def now_utc() -> datetime:
    """UTC now（naive）。

    SQLite 的 `DATETIME` 不保存时区，统一以 UTC 存 ISO8601 字符串（0.2.1）；
    对外输出时由 `schemas/common.py` 的序列化器补 `Z`。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class TimestampMixin:
    """所有业务表公共的 `created_at` / `updated_at`（2.2）。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


UUIDStr = Annotated[str, mapped_column(String(ULID_LENGTH), primary_key=True, default=new_ulid)]
"""主键：26 位 ULID（0.2.1 / 2.2）。外键列请 `mapped_column(String(26), ForeignKey(...))`。"""

JSONDict = Annotated[dict[str, Any], mapped_column(JSON, default=dict)]
"""JSON 对象列（如 `agents.tool_ids` 的容器、`runs.input`）。"""

JSONList = Annotated[list[Any], mapped_column(JSON, default=list)]
"""JSON 数组列（如 `agents.tool_ids`、`runs.tool_sequence`）。"""
