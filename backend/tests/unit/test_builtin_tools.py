"""5 个内置工具（详细设计 2.5 / 4.2.4 / 7.3 的测试要求）。

`test_tool_executor.py` 用 `EchoTool` 验证九步流水线的**通用**分支；本模块逐个验证**内置工具本身**
的行为边界：

| 工具 | 本模块锁定的点 |
|---|---|
| `calculator` | AST 白名单（拒属性 / 名字 / 字符串 / lambda / 下标）、指数与操作数上限、除零 |
| `file_read` | 沙箱内可读、穿越被拒、缺失 / 非 UTF-8 / 超大文件、按 `max_bytes` 截断 |
| `file_write` | append / overwrite、只写 `uploads|outputs`、拒绝对路径与超限内容、mode 白名单 |
| `web_search` | 无 Key → `TOOL_DISABLED`、网络闸门、两家后端归一、HTTP / 非 JSON 失败 |
| `python_execute` | AST 检查、子进程执行、**超时被 kill**、非零退出码、输出截断 |

所有网络访问都走 `httpx.MockTransport`（9.2：测试禁止真实外网）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.enums import SpanType
from app.core.errors import (
    ErrorCode,
    ToolDisabledError,
    ToolExecutionFailedError,
    ToolInvalidArgumentsError,
    ToolSandboxViolationError,
    ToolTimeoutError,
)
from app.runtime.observability.tracer import Span
from app.runtime.tools.base import ToolContext
from app.runtime.tools.builtin.calculator import MAX_EXPRESSION_CHARS, MAX_OPERAND, CalculatorTool
from app.runtime.tools.builtin.file_read import FileReadTool
from app.runtime.tools.builtin.file_write import FileWriteTool
from app.runtime.tools.builtin.python_execute import (
    MAX_CODE_CHARS,
    MAX_STREAM_BYTES,
    PythonExecuteTool,
    check_code,
)
from app.runtime.tools.builtin.web_search import WebSearchTool

CALCULATOR = CalculatorTool()
FILE_READ = FileReadTool()
FILE_WRITE = FileWriteTool()
PYTHON_EXECUTE = PythonExecuteTool()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """沙箱根（`uploads/` 供 `file_read` 的样例文件）。"""
    resolved = tmp_path / "files"
    (resolved / "uploads").mkdir(parents=True)
    return resolved


def _settings(**overrides: Any) -> Settings:
    """测试用 `Settings`（可覆盖 `web_search_*` / 输出上限等）。"""
    return Settings(app_env="test", **overrides)


def _ctx(root: Path, settings: Settings | None = None, **overrides: Any) -> ToolContext:
    """一个独立的 `ToolContext`（span 手搓：本模块不关心 Trace 落库）。"""
    return ToolContext(
        run_id="run-unit",
        trace_span=Span(
            span_id="span-unit",
            trace_id="trace-unit",
            run_id="run-unit",
            parent_span_id=None,
            span_type=SpanType.TOOL,
            name="tool:unit",
            seq=2,
        ),
        sandbox_root=root,
        settings=settings or _settings(),
        cancellation=asyncio.Event(),
        **overrides,
    )


# --------------------------------------------------------------------------------------
# calculator：AST 白名单（4.2.4：禁止 eval / 属性访问 / 变量）
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2+2", "2+2 = 4"),
        ("(12+3)*4/sqrt(9)", "(12+3)*4/sqrt(9) = 20"),
        ("2 ** 10", "2 ** 10 = 1024"),
        ("17 % 5", "17 % 5 = 2"),
        ("7 // 2", "7 // 2 = 3"),
        ("-3 + +4", "-3 + +4 = 1"),
        ("round(2.567, 2)", "round(2.567, 2) = 2.57"),
        ("max(1, 2, 3)", "max(1, 2, 3) = 3"),
        ("  pi  ", "pi = 3.14159"),
    ],
)
async def test_calculator_evaluates_whitelisted_expressions(root: Path, expression: str, expected: str) -> None:
    result = await CALCULATOR.run(_ctx(root), expression=expression)

    assert result.content == expected
    assert result.is_error is False
    assert isinstance(result.meta["result"], (int, float))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",  # 名字不在白名单 / 以 __ 开头
        "open('/etc/passwd')",  # 未白名单函数
        "1 + 'a'",  # 字符串常量
        "True",  # 布尔常量
        "(1).__class__",  # 属性访问
        "[1, 2][0]",  # 下标
        "(lambda: 1)()",  # lambda
        "{'a': 1}",  # 字典
        "x + 1",  # 未知名字
        "1 if True else 2",  # IfExp
        "bool(1)",  # 非白名单函数
        "print(1)",  # 非纯函数
        "9 ** 9 ** 9",  # 指数爆表
        "1e13 + 1",  # 操作数超上限（MAX_OPERAND = 1e12）
    ],
)
async def test_calculator_rejects_unsafe_expressions(root: Path, expression: str) -> None:
    with pytest.raises(ToolInvalidArgumentsError) as caught:
        await CALCULATOR.run(_ctx(root), expression=expression)

    assert caught.value.code == ErrorCode.TOOL_INVALID_ARGUMENTS
    assert caught.value.http_status == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["", "   ", "1/0", "sqrt(-1)", "1 +"])
async def test_calculator_argument_and_math_errors(root: Path, expression: str) -> None:
    with pytest.raises((ToolInvalidArgumentsError, ToolExecutionFailedError)) as caught:
        await CALCULATOR.run(_ctx(root), expression=expression)

    assert caught.value.code in {ErrorCode.TOOL_INVALID_ARGUMENTS, ErrorCode.TOOL_EXECUTION_FAILED}


@pytest.mark.asyncio
async def test_calculator_caps_operand_and_expression_length(root: Path) -> None:
    assert MAX_OPERAND == 1e12
    with pytest.raises(ToolInvalidArgumentsError):
        await CALCULATOR.run(_ctx(root), expression="x" * (MAX_EXPRESSION_CHARS + 1))

    # 常量级上限：单个字面量不能超过 MAX_OPERAND
    with pytest.raises(ToolInvalidArgumentsError):
        await CALCULATOR.run(_ctx(root), expression="99999999999999")


# --------------------------------------------------------------------------------------
# file_read：沙箱内可读 + 穿越被拒 + 大小 / 编码 / 截断
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_file_read_returns_text_and_relative_path(root: Path) -> None:
    # 用 `write_bytes` 避开 Windows 的换行翻译（`write_text` 会把 `\n` 变成 `\r\n`）
    (root / "uploads" / "notes.txt").write_bytes("第一行\n第二行".encode())

    result = await FILE_READ.run(_ctx(root), path="uploads/notes.txt")

    assert result.content == "第一行\n第二行"
    assert result.meta == {"path": "uploads/notes.txt", "size_bytes": 19, "truncated": False}


@pytest.mark.asyncio
async def test_file_read_truncates_by_max_bytes(root: Path) -> None:
    (root / "uploads" / "long.txt").write_text("x" * 100, encoding="utf-8")

    result = await FILE_READ.run(_ctx(root), path="uploads/long.txt", max_bytes=10)

    assert result.content == "x" * 10
    assert result.meta["truncated"] is True
    assert result.meta["size_bytes"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../../../etc/passwd", "uploads/../../secret.txt"])
async def test_file_read_rejects_traversal(root: Path, path: str) -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        await FILE_READ.run(_ctx(root), path=path)

    assert caught.value.code == ErrorCode.TOOL_SANDBOX_VIOLATION
    assert caught.value.http_status == 403


@pytest.mark.asyncio
async def test_file_read_rejects_missing_binary_and_oversized_files(root: Path) -> None:
    with pytest.raises(ToolExecutionFailedError) as missing:
        await FILE_READ.run(_ctx(root), path="uploads/nope.txt")
    assert "File not found" in missing.value.message

    (root / "uploads" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ToolExecutionFailedError) as binary:
        await FILE_READ.run(_ctx(root), path="uploads/blob.bin")
    assert "not valid UTF-8" in binary.value.message

    # 5 MB 上限：只看 stat 大小，不读进内存
    (root / "uploads" / "huge.txt").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ToolExecutionFailedError) as huge:
        await FILE_READ.run(_ctx(root), path="uploads/huge.txt")
    assert huge.value.details["size_bytes"] == 5 * 1024 * 1024 + 1


@pytest.mark.asyncio
async def test_file_read_clamps_max_bytes_by_permission_ceiling(root: Path) -> None:
    """工具级 `max_output_bytes` 是硬上限：`max_bytes` 只能更小（4.2.3 步骤 7）。"""
    (root / "uploads" / "long.txt").write_text("y" * 100, encoding="utf-8")

    ctx = _ctx(root, max_output_bytes=20)
    result = await FILE_READ.run(ctx, path="uploads/long.txt", max_bytes=1_000)

    assert result.content == "y" * 20 and result.meta["truncated"] is True

    with pytest.raises(ToolInvalidArgumentsError):
        await FILE_READ.run(_ctx(root), path="uploads/long.txt", max_bytes=0)


# --------------------------------------------------------------------------------------
# file_write：只写 uploads/ / outputs/，append / overwrite
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_file_write_appends_then_overwrites(root: Path) -> None:
    ctx = _ctx(root)

    first = await FILE_WRITE.run(ctx, path="outputs/report.md", content="line-1\n")
    second = await FILE_WRITE.run(ctx, path="outputs/report.md", content="line-2\n")
    overwritten = await FILE_WRITE.run(ctx, path="outputs/report.md", content="done", mode="overwrite")

    assert (first.meta["mode"], first.meta["bytes_written"]) == ("append", 7)
    assert second.content == "Wrote 7 bytes to outputs/report.md (mode=append)"
    assert overwritten.meta["bytes_written"] == 4
    assert (root / "outputs" / "report.md").read_text(encoding="utf-8") == "done"
    assert (root / "uploads").is_dir()  # 未写 uploads 也不会被删


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("logs/a.txt", "SUBDIR"),
        ("secrets.txt", "SUBDIR"),
        ("outputs/../secrets.txt", "SUBDIR"),
        ("../../../etc/passwd", "OUTSIDE"),
    ],
)
async def test_file_write_rejects_paths_outside_uploads_and_outputs(root: Path, path: str, reason: str) -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        await FILE_WRITE.run(_ctx(root), path=path, content="x")

    assert caught.value.code == ErrorCode.TOOL_SANDBOX_VIOLATION
    if reason == "SUBDIR":
        assert caught.value.details["allowed_subdirs"] == ["uploads", "outputs"]
    assert not (root / "secrets.txt").exists()


@pytest.mark.asyncio
async def test_file_write_rejects_absolute_path_content_limit_and_bad_mode(root: Path) -> None:
    safe_root = root

    with pytest.raises(ToolSandboxViolationError) as absolute:
        await FILE_WRITE.run(_ctx(root), path=str(safe_root / "outputs" / "a.txt"), content="x")
    assert "Absolute paths are not writable" in absolute.value.message

    with pytest.raises(ToolSandboxViolationError) as too_big:
        await FILE_WRITE.run(_ctx(root), path="outputs/big.txt", content="x" * (1024 * 1024 + 1))
    assert too_big.value.details["limit_bytes"] == 1024 * 1024

    with pytest.raises(ToolInvalidArgumentsError) as bad_mode:
        await FILE_WRITE.run(_ctx(root), path="outputs/a.txt", content="x", mode="truncate")
    assert bad_mode.value.details["mode"] == "truncate"


# --------------------------------------------------------------------------------------
# web_search：无 Key 降级 + 网络闸门 + 两家后端归一（全部走 MockTransport）
# --------------------------------------------------------------------------------------
def _client_factory(handler: Any, calls: list[httpx.Request]) -> Any:
    """`httpx.MockTransport` 工厂（9.2：测试不允许真实外网）。"""

    def factory(timeout: float) -> httpx.AsyncClient:
        def record(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(record), timeout=timeout)

    return factory


def _search_ctx(root: Path, **overrides: Any) -> ToolContext:
    """`web_search` 需要出网：默认给"显式允许 + host 白名单"。"""
    overrides.setdefault("allow_network", True)
    overrides.setdefault("allowed_hosts", ("api.tavily.com", "google.serper.dev"))
    return _ctx(root, **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        _settings(),
        _settings(web_search_provider="tavily"),
        _settings(web_search_api_key="key-only"),
        _settings(web_search_provider="duckduckgo", web_search_api_key="k"),
    ],
)
async def test_web_search_degrades_to_tool_disabled_without_config(root: Path, settings: Settings) -> None:
    """2.5：未配置 Provider / Key（或 Provider 不支持）→ `TOOL_DISABLED`，不是 500。"""
    with pytest.raises(ToolDisabledError) as caught:
        await WebSearchTool().run(_search_ctx(root, settings=settings), query="FastAPI")

    assert caught.value.code == ErrorCode.TOOL_DISABLED
    assert caught.value.http_status == 409


@pytest.mark.asyncio
async def test_web_search_tavily_normalizes_results(root: Path) -> None:
    calls: list[httpx.Request] = []
    payload = {
        "results": [
            {"title": "FastAPI", "url": "https://fastapi.tiangolo.com", "content": "docs"},
            {"title": "Release", "url": "https://example.com/rel", "content": "notes"},
        ]
    }
    tool = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(200, json=payload), calls))
    settings = _settings(web_search_provider="tavily", web_search_api_key="tvly-secret")

    result = await tool.run(_search_ctx(root, settings=settings), query="FastAPI 0.115", max_results=1)

    request = calls[0]
    assert request.url == "https://api.tavily.com/search"
    assert json.loads(request.content.decode("utf-8")) == {
        "api_key": "tvly-secret",
        "query": "FastAPI 0.115",
        "max_results": 1,
    }
    assert request.headers["Content-Type"] == "application/json"
    assert result.meta == {"provider": "tavily", "hit_count": 1}
    assert result.content == {
        "query": "FastAPI 0.115",
        "provider": "tavily",
        "results": [{"title": "FastAPI", "url": "https://fastapi.tiangolo.com", "snippet": "docs"}],
    }


@pytest.mark.asyncio
async def test_web_search_serper_uses_api_key_header(root: Path) -> None:
    calls: list[httpx.Request] = []
    payload = {"organic": [{"title": "T", "link": "https://b.example", "snippet": "s"}]}
    tool = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(200, json=payload), calls))
    settings = _settings(web_search_provider="serper", web_search_api_key="serper-key")

    result = await tool.run(_search_ctx(root, settings=settings), query="abc")

    request = calls[0]
    assert request.url == "https://google.serper.dev/search"
    assert request.headers["X-API-KEY"] == "serper-key"
    assert json.loads(request.content.decode("utf-8")) == {"q": "abc", "num": 5}
    assert result.content["results"] == [{"title": "T", "url": "https://b.example", "snippet": "s"}]


@pytest.mark.asyncio
async def test_web_search_network_gate_blocks_before_any_request(root: Path) -> None:
    calls: list[httpx.Request] = []
    tool = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(200, json={}), calls))
    settings = _settings(web_search_provider="tavily", web_search_api_key="k")

    with pytest.raises(ToolSandboxViolationError) as blocked:
        await tool.run(_ctx(root, settings=settings), query="abc")  # allow_network 默认 False
    assert blocked.value.details["reason"] == "NETWORK_DISABLED"

    with pytest.raises(ToolSandboxViolationError):
        await tool.run(_search_ctx(root, settings=settings, allowed_hosts=[]), query="abc")
    with pytest.raises(ToolSandboxViolationError):
        await tool.run(_search_ctx(root, settings=settings, allowed_hosts=["api.example.com"]), query="abc")

    assert calls == []  # 任何一次请求都没发出去


@pytest.mark.asyncio
async def test_web_search_maps_upstream_failures(root: Path) -> None:
    settings = _settings(web_search_provider="tavily", web_search_api_key="k")

    failing = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(503, text="nope"), []))
    with pytest.raises(ToolExecutionFailedError) as http_error:
        await failing.run(_search_ctx(root, settings=settings), query="abc")
    assert http_error.value.details["status_code"] == 503

    non_json = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(200, text="<html>"), []))
    with pytest.raises(ToolExecutionFailedError) as parse_error:
        await non_json.run(_search_ctx(root, settings=settings), query="abc")
    assert "non-JSON" in parse_error.value.message


@pytest.mark.asyncio
async def test_web_search_validates_arguments(root: Path) -> None:
    settings = _settings(web_search_provider="tavily", web_search_api_key="k")
    tool = WebSearchTool(client_factory=_client_factory(lambda request: httpx.Response(200, json={}), []))

    with pytest.raises(ToolInvalidArgumentsError) as empty:
        await tool.run(_search_ctx(root, settings=settings), query="   ")
    assert empty.value.details["query"] == "   "

    for limit in (0, 11, -1):
        with pytest.raises(ToolInvalidArgumentsError) as bad_limit:
            await tool.run(_search_ctx(root, settings=settings), query="abc", max_results=limit)
        assert bad_limit.value.details["max_results"] == limit


# --------------------------------------------------------------------------------------
# python_execute：AST 白名单 + 子进程 + 超时 kill（7.3 测试要求）
# --------------------------------------------------------------------------------------
def test_python_execute_ast_whitelist() -> None:
    assert check_code("import math\nprint(math.sqrt(9))") == []
    assert check_code("from collections import Counter\nprint(Counter('aab'))") == []
    assert check_code("x = 1 + 1\nprint(x)") == []

    assert check_code("import os") == ["import of 'os' is not allowed"]
    assert check_code("from subprocess import run") == ["import of 'subprocess' is not allowed"]
    assert check_code("open('/etc/passwd')") == ["name 'open' is not allowed"]
    assert check_code("eval('1+1')") == ["name 'eval' is not allowed"]
    assert check_code("().__class__") == ["dunder attribute '__class__' is not allowed"]
    assert check_code("def f():\n    global x\n    x = 1") == ["global statement is not allowed"]
    assert check_code("def f(")[0].startswith("syntax error:")


@pytest.mark.asyncio
async def test_python_execute_runs_code_in_subprocess(root: Path) -> None:
    result = await PYTHON_EXECUTE.run(_ctx(root), code="import math\nprint(int(math.sqrt(9)))")

    assert result.is_error is False
    assert result.meta["exit_code"] == 0
    assert result.content["exit_code"] == 0
    assert result.content["stdout"].strip() == "3"  # Windows 子进程输出 `\r\n`，按行内容断言
    assert result.content["stderr"] == "" and result.content["truncated"] is False


@pytest.mark.asyncio
async def test_python_execute_reports_nonzero_exit_code(root: Path) -> None:
    result = await PYTHON_EXECUTE.run(_ctx(root), code="print(1 / 0)")

    assert result.is_error is True
    assert result.meta["exit_code"] == 1
    assert "ZeroDivisionError" in result.content["stderr"]
    assert result.content["stdout"] == ""


@pytest.mark.asyncio
async def test_python_execute_kills_subprocess_on_timeout(root: Path) -> None:
    """4.2.4 第 4 条：超时 `kill()` 子进程 —— 死循环必须在秒级被掐掉，而不是拖满 Run。"""
    started = perf_counter()

    with pytest.raises(ToolTimeoutError) as caught:
        await PYTHON_EXECUTE.run(_ctx(root), code="while True:\n    pass", timeout_seconds=0.3)

    elapsed = perf_counter() - started
    assert caught.value.code == ErrorCode.TOOL_TIMEOUT
    assert caught.value.http_status == 504
    assert caught.value.details["timeout_seconds"] == 0.3
    assert elapsed < 10.0, f"timeout was not enforced (took {elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_python_execute_truncates_and_validates_input(root: Path) -> None:
    result = await PYTHON_EXECUTE.run(_ctx(root), code="print('x' * 20000)")

    assert result.meta["truncated"] is True
    assert len(result.content["stdout"].encode("utf-8")) <= MAX_STREAM_BYTES

    with pytest.raises(ToolInvalidArgumentsError) as empty:
        await PYTHON_EXECUTE.run(_ctx(root), code="   ")
    assert empty.value.details["code"] == "   "

    with pytest.raises(ToolInvalidArgumentsError) as too_long:
        await PYTHON_EXECUTE.run(_ctx(root), code="x" * (MAX_CODE_CHARS + 1))
    assert too_long.value.details["length"] == MAX_CODE_CHARS + 1

    with pytest.raises(ToolInvalidArgumentsError) as forbidden:
        await PYTHON_EXECUTE.run(_ctx(root), code="import os\nprint(os.listdir('.'))")
    assert forbidden.value.details["problems"] == ["import of 'os' is not allowed"]


def test_builtin_default_permissions_match_design_table() -> None:
    """2.5 的内置工具表：`calculator` safe / `file_write` 需开启 / `python_execute` dangerous 默认禁用。"""
    by_name = {tool.name: tool for tool in (CALCULATOR, FILE_READ, FILE_WRITE, WebSearchTool(), PYTHON_EXECUTE)}

    assert str(by_name["calculator"].default_permission.level) == "safe"
    assert by_name["calculator"].default_permission.require_approval is False
    assert str(by_name["file_read"].default_permission.level) == "safe"
    assert str(by_name["file_write"].default_permission.level) == "guarded"
    assert by_name["file_write"].default_permission.require_approval is True
    assert str(by_name["web_search"].default_permission.level) == "guarded"
    assert by_name["web_search"].default_permission.allow_network is True
    assert str(by_name["python_execute"].default_permission.level) == "dangerous"
    assert by_name["python_execute"].default_permission.require_approval is True
