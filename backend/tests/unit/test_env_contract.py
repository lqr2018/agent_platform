"""文档 ↔ 代码一致性：环境变量（《详细设计》附录 C ↔ `Settings` ↔ `.env.example`）。

附录 C 是环境变量的**全量清单**（"`.env.example` 必须与本表逐项对应（含默认值），
由 CI 做键名一致性检查"），本文件把这句承诺固化成测试。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "详细设计.md"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

APPENDIX_C_ANCHOR = "### 附录 C：环境变量全量清单"
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _appendix_c_env_keys() -> set[str]:
    """解析附录 C 的表格：跳到第一个表格行，收集到表格结束（或下一个标题）。"""
    lines = DOC_PATH.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(APPENDIX_C_ANCHOR))
    keys: set[str] = set()
    in_table = False
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break  # 到了下一个小节
        if not line.startswith("|"):
            if in_table:
                break  # 表格结束
            continue  # 表格之前的说明段落
        in_table = True
        cell = line.split("|")[1].strip().strip("`")
        if "~" in cell:  # 划掉的行 = Backlog 变量（如 ~~MCP_TRANSPORT_ENABLED~~）
            continue
        if ENV_KEY.match(cell):
            keys.add(cell)
    assert keys, "未能从附录 C 解析出任何环境变量"
    return keys


def _dotenv_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_settings_fields_match_appendix_c() -> None:
    expected = _appendix_c_env_keys()
    actual = {name.upper() for name in Settings.model_fields}
    assert actual == expected, f"仅文档有：{expected - actual}；仅代码有：{actual - expected}"


def test_env_example_matches_appendix_c() -> None:
    expected = _appendix_c_env_keys()
    actual = _dotenv_keys()
    assert actual == expected, f"仅文档有：{expected - actual}；仅 .env.example 有：{actual - expected}"
