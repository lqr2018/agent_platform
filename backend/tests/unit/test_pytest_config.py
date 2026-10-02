"""pytest 调用形态的回归测试（6.4 / 9.3 / 附录 F v1.8）。

背景（一次真实的 CI 回归）：CI 用**控制台脚本** `pytest` 调用——它**不会**把 CWD 加进
`sys.path`；而本地与文档用的是 `python -m pytest`——它**会**加。集成测试写的是
`from tests.helpers import ...`，于是同一份代码在 CI 上 9 个集成测试在收集期
ImportError（`Interrupted: 9 errors during collection`）→ pytest 退出码 2，本地却"全绿"。

修复方式：`pyproject.toml` 的 `[tool.pytest.ini_options]` 加 `pythonpath = ["."]`，
让两种调用形态的 `sys.path` 一致。
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_pytest_config_keeps_backend_root_on_sys_path() -> None:
    """`pythonpath` 必须含 `.`（相对 rootdir=backend/），否则控制台脚本形态会崩。"""
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "." in config["tool"]["pytest"]["ini_options"].get("pythonpath", [])


def test_tests_namespace_is_importable() -> None:
    """`tests` 是命名空间包（无 `__init__.py`），只有 backend/ 在 `sys.path` 上才找得到。"""
    assert importlib.util.find_spec("tests.helpers") is not None
