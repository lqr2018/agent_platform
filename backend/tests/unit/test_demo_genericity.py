"""8.5 的"零专属代码"验收：三大 Demo 的差异**只能**出现在配置里。

做法是**扫描**而不是断言行为：AST 解析 `app/**/*.py`，把**非 docstring 的字符串字面量**
拿去比对 Demo 侧标识符（Agent 名、Workflow 配置名、示例 KB 名）。一旦有人写出
`if agent.name == "Research Agent"` 之类的分支，这条用例立刻变红 —— 这正是 8.5 想避免的东西
（"能力来自平台装配，不来自为 Demo 写的代码"）。

口径说明：

- **注释与 docstring 不算**：注释不在 AST 里，docstring 显式跳过 —— 示例文本（如
  `python -m app.scripts.seed_demo` 的用法、`workflow_config.py` 的 YAML 片段）出现在文档里是好事；
- 扫描范围是整个 `app/`（含 `app/scripts/`）：`seed_demo` 只做"YAML → API 入参"的翻译，
  它自己也不该硬编码任何 Demo 名字。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

FORBIDDEN_TOKENS = (
    "Research Agent",
    "Data Analysis Agent",
    "Knowledge Assistant",
    "research-agent",
    "data-analysis-agent",
    "knowledge-assistant",
    "research-flow",
    "kb-qa-flow",
    "product-docs",
)
"""Demo 侧的标识符（8.2 / 8.3 / 8.4 的名字与 7.4 的示例图名）。"""


def app_python_files() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """模块 / 类 / 函数体第一条语句若是字符串常量，就是 docstring（这些节点的 id）。"""
    found: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            found.add(id(first.value))
    return found


def code_string_literals(path: Path) -> list[tuple[int, str]]:
    """返回 `(行号, 文本)`：非 docstring 的字符串字面量（f-string 的常量片段也算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    literals: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            literals.append((node.lineno, node.value))
    return literals


def test_app_has_no_demo_specific_identifiers() -> None:
    offenders = [
        f"{path.relative_to(APP_ROOT)}:{line}: {token}"
        for path in app_python_files()
        for line, text in code_string_literals(path)
        for token in FORBIDDEN_TOKENS
        if token in text
    ]
    assert offenders == [], f"Demo 名字出现在生产代码里（8.5 要求改为配置驱动）：{offenders}"


def test_demo_genericity_scan_is_not_vacuous() -> None:
    """反向自检：扫描确实读到了代码里的字符串（否则"没命中"可能只是扫描器坏了）。"""
    literals = [text for path in app_python_files() for _, text in code_string_literals(path)]
    assert len(literals) > 200, f"扫描器只读到 {len(literals)} 个字面量，检查 AST 过滤逻辑"
