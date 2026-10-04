"""`python_execute` 内置工具（详细设计 2.5 / 4.2.4 第 4 条 + SD-17）。

> **明确声明：这不是安全边界**（4.2.4 原文）。它的定位是"挡住常见误用 + 隔离进程 + 限时"，
> 真正的边界是 `PYTHON_EXECUTE_ENABLED`（默认 false，需显式开启）与容器化部署（Backlog）。

三层防护：

1. **AST 白名单**：禁 `import`（除 `ALLOWED_MODULES`）、禁 `open` / `eval` / `exec` / `compile`
   / `__import__` / `input` / `getattr` 等名字、禁任何 `__` 开头的属性与标识符；
2. **子进程隔离**：`python -I -S -c <code>`（忽略环境变量与用户 site-packages）；
3. **超时与输出上限**：超时 `kill()` 子进程；stdout / stderr 各自截断。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from collections.abc import Mapping
from typing import Any

from app.core.enums import PermissionLevel
from app.core.errors import ToolInvalidArgumentsError, ToolTimeoutError
from app.runtime.tools import sandbox
from app.runtime.tools.base import BaseTool, ToolContext, ToolPermissionConfig, ToolResult

ALLOWED_MODULES = frozenset(
    {"math", "statistics", "json", "re", "datetime", "random", "itertools", "collections", "decimal"}
)
FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "memoryview",
        "breakpoint",
        "help",
        "exit",
        "quit",
    }
)
MAX_CODE_CHARS = 4_000
MAX_STREAM_BYTES = 16_384
MAX_TIMEOUT_SECONDS = 10.0


class PythonExecuteTool(BaseTool):
    """4.2.4 的 `python_execute`（默认禁用）。"""

    name = "python_execute"
    display_name = "Python 执行"
    description = (
        "在隔离子进程中执行一小段 Python 代码，返回 exit_code 与 stdout/stderr。"
        "仅允许标准库中的少量模块（math/statistics/json/re/datetime/random/itertools/"
        "collections/decimal），禁止文件、网络与系统调用；该工具默认关闭。"
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码（用 print 输出结果）",
                "minLength": 1,
                "maxLength": MAX_CODE_CHARS,
            },
            "timeout_seconds": {
                "type": "number",
                "description": f"超时秒数（默认取平台配置，最多 {MAX_TIMEOUT_SECONDS:g} 秒）",
                "minimum": 0.1,
                "maximum": MAX_TIMEOUT_SECONDS,
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "exit_code": {"type": "integer"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
        },
    }
    default_permission = ToolPermissionConfig(
        level=PermissionLevel.DANGEROUS,
        require_approval=True,
        timeout_seconds=MAX_TIMEOUT_SECONDS,
        max_calls_per_run=5,
    )

    async def run(
        self, ctx: ToolContext, *, code: str = "", timeout_seconds: float | None = None, **_: Any
    ) -> ToolResult:
        text = code or ""
        if not text.strip():
            raise ToolInvalidArgumentsError("`code` must not be empty", details={"code": code})
        if len(text) > MAX_CODE_CHARS:
            raise ToolInvalidArgumentsError(
                f"`code` must be at most {MAX_CODE_CHARS} characters", details={"length": len(text)}
            )

        problems = check_code(text)
        if problems:
            raise ToolInvalidArgumentsError(
                "Code failed the safety check",
                details={"problems": problems[:10], "allowed_modules": sorted(ALLOWED_MODULES)},
            )

        timeout = min(timeout_seconds or ctx.timeout_seconds, MAX_TIMEOUT_SECONDS)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await _terminate(process)
            raise ToolTimeoutError(
                f"Code execution exceeded {timeout:g} seconds", details={"timeout_seconds": timeout}
            ) from exc

        exit_code = int(process.returncode or 0)
        out_text, out_truncated = sandbox.truncate_text(
            stdout.decode("utf-8", errors="replace"), max_bytes=MAX_STREAM_BYTES
        )
        err_text, err_truncated = sandbox.truncate_text(
            stderr.decode("utf-8", errors="replace"), max_bytes=MAX_STREAM_BYTES
        )
        return ToolResult(
            content={
                "exit_code": exit_code,
                "stdout": out_text,
                "stderr": err_text,
                "truncated": out_truncated or err_truncated,
            },
            is_error=exit_code != 0,
            meta={"exit_code": exit_code, "truncated": out_truncated or err_truncated},
        )


def check_code(code: str) -> list[str]:
    """AST 白名单检查；返回问题清单（空 = 通过）。"""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]

    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name, problems)
        elif isinstance(node, ast.ImportFrom):
            _check_module(node.module or "", problems)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"name '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"dunder attribute '{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"{type(node).__name__.lower()} statement is not allowed")
    return problems


def _check_module(module: str, problems: list[str]) -> None:
    root = module.split(".", maxsplit=1)[0]
    if root not in ALLOWED_MODULES:
        problems.append(f"import of '{root}' is not allowed")


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """超时后杀子进程（4.2.4 第 4 条：杀进程组；Windows 上 `kill()` 即 TerminateProcess）。"""
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - 进程已退出
        return
    await process.wait()
