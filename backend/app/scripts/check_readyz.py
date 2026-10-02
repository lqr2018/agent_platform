"""命令行就绪探针（详细设计 6.3）：连库 + 可写 + 迁移在 head，失败即非 0 退出。

用途：

- CI 在**干净检出**上验证"迁移 + 应用启动路径"都能自愈（`data/` 目录不在版本库里，
  由 `app/db/session.py::ensure_sqlite_directory()` 自动创建）；
- 容器/服务器上手工排查（等价于 `GET /readyz` 的检查项，但不需要起 HTTP 服务）。

用法（在 `backend/` 下）：

    python -m app.scripts.check_readyz
"""

from __future__ import annotations

import asyncio
import sys

from app.db.session import check_database, dispose_engine


async def _probe() -> tuple[bool, str]:
    try:
        return await check_database()
    finally:
        await dispose_engine()


def main() -> int:
    ok, detail = asyncio.run(_probe())
    print(f"readyz ok={ok} detail={detail}")  # noqa: T201
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
