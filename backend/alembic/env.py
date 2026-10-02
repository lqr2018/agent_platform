"""Alembic 环境（详细设计 2.14 / 7.1 任务 4）。

要点：

- 连接串从 `app.core.config.Settings` 注入（1.4：唯一配置入口，**不**在 alembic.ini 里写 URL）；
- `render_as_batch=True`：SQLite 加约束/改列时自动走 `batch_alter_table`（2.14）；
- **任何环境都不调用 `create_all()`**：`Base.metadata` 只作为 autogenerate 的输入（2.14）；
- 跨库（PG）的 `JSONB` 类型装饰器属"记录不实现"（SD-8：只保证 SQLite）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db import models  # noqa: F401 - 让 autogenerate 看到全部表（2.14）
from app.db.base import Base

# Phase 1 起导入模型包；后续 Phase 新增的模型只需加到 `app/db/models/__init__.py`。

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 只在"未显式配置 URL"时回落到 Settings：测试与脚本会通过 `Config.set_main_option`
# 指定临时库，必须被尊重（否则迁移跑到默认库上，测试会失真）。
# `%%` 转义：alembic 的 ini 会做 % 插值。
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata

COMMON_CONTEXT_OPTIONS = {
    "target_metadata": target_metadata,
    "render_as_batch": True,
    "compare_type": True,
    "compare_server_default": True,
}


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`：只生成 SQL，不连库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMMON_CONTEXT_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **COMMON_CONTEXT_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
