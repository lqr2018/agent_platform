"""导出 OpenAPI 规范到 `docs/openapi.json`（详细设计 3.5）。

前端 `npm run gen:api`（`openapi-typescript`）以该文件为输入生成 `src/types/api.d.ts`，
CI 校验"生成结果与提交版本一致"。用法（在 `backend/` 下）：

    python -m app.scripts.export_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_PATH = REPO_ROOT / "docs" / "openapi.json"


def main() -> None:
    spec = create_app().openapi()
    OUTPUT_PATH.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_PATH} ({len(spec.get('paths', {}))} paths)")  # noqa: T201


if __name__ == "__main__":
    main()
