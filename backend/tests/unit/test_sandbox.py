"""沙箱四类限制（详细设计 4.2.4 / 7.3 的测试要求：路径穿越与符号链接）。

`test_tool_executor.py` 覆盖的是"执行器把沙箱错误转成 `TOOL_SANDBOX_VIOLATION`"这条**链路**；
本模块只测 `sandbox.py` 的**纯函数**，把 `../` 穿越、符号链接逃逸、写入目录白名单、
大小闸门与网络闸门（含 SSRF）逐条钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ErrorCode, ToolSandboxViolationError
from app.runtime.tools import sandbox

TRAVERSAL = "../../../etc/passwd"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """沙箱根（与 `SANDBOX_ROOT` 同构：真实目录 + 两个可写子目录）。"""
    resolved = tmp_path / "files"
    (resolved / "uploads").mkdir(parents=True)
    (resolved / "outputs").mkdir(parents=True)
    return resolved


def _violation(error: ToolSandboxViolationError, *, path: str) -> dict[str, object]:
    assert error.code == ErrorCode.TOOL_SANDBOX_VIOLATION
    assert error.http_status == 403
    assert error.details.get("path") == path
    return error.details


# --------------------------------------------------------------------------------------
# 第 1 条：路径（resolve 之后必须落在沙箱根 / 白名单内）
# --------------------------------------------------------------------------------------
def test_readable_accepts_paths_inside_sandbox(root: Path) -> None:
    (root / "uploads" / "notes.txt").write_text("hi", encoding="utf-8")

    assert sandbox.resolve_readable(root, "uploads/notes.txt") == (root / "uploads" / "notes.txt").resolve()
    assert sandbox.resolve_readable(root, "uploads/../uploads/notes.txt") == (root / "uploads" / "notes.txt").resolve()
    assert sandbox.resolve_readable(root, str(root / "uploads" / "notes.txt")).is_file()


def test_readable_rejects_dotdot_traversal(root: Path) -> None:
    """7.3 测试要求：`../../../etc/passwd` 必须被拒（在 `resolve()` **之后**判定）。"""
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.resolve_readable(root, TRAVERSAL)

    details = _violation(caught.value, path=TRAVERSAL)
    assert details["sandbox_root"] == str(root)
    assert details["allowed_paths"] == []


def test_readable_rejects_sibling_directory_with_shared_prefix(root: Path) -> None:
    """前缀相同的兄弟目录不算"在沙箱内"（按路径分量判定，而不是字符串前缀）。"""
    (root.parent / "files_backup").mkdir()
    (root.parent / "files_backup" / "secret.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ToolSandboxViolationError):
        sandbox.resolve_readable(root, "../files_backup/secret.txt")


def test_readable_rejects_symlink_escape(root: Path, tmp_path: Path) -> None:
    """符号链接指向沙箱外 → 拒绝（`resolve()` 展开链接后再判定，4.2.4 原文）。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "uploads" / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 需开发者模式 / Linux 容器权限
        pytest.skip("this platform does not allow creating symlinks without extra privileges")

    with pytest.raises(ToolSandboxViolationError):
        sandbox.resolve_readable(root, "uploads/link.txt")


def test_readable_allows_whitelisted_directory_outside_root(root: Path, tmp_path: Path) -> None:
    """`allowed_paths` 是沙箱之外的**额外**白名单（2.5 / 4.2.4）。"""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("ok", encoding="utf-8")

    resolved = sandbox.resolve_readable(root, str(shared / "a.txt"), allowed_paths=[str(shared)])

    assert resolved == (shared / "a.txt").resolve()
    with pytest.raises(ToolSandboxViolationError):
        sandbox.resolve_readable(root, str(shared / "a.txt"))


@pytest.mark.parametrize("raw", ["", "   "])
def test_readable_rejects_empty_path(root: Path, raw: str) -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.resolve_readable(root, raw)
    assert caught.value.details["path"] == raw


# --------------------------------------------------------------------------------------
# 第 2 条：目录（写入仅限 uploads/ / outputs/，且拒绝绝对路径）
# --------------------------------------------------------------------------------------
def test_writable_requires_uploads_or_outputs_subdir(root: Path) -> None:
    assert sandbox.resolve_writable(root, "uploads/a.txt") == (root / "uploads" / "a.txt").resolve()
    assert sandbox.resolve_writable(root, "outputs/report.md") == (root / "outputs" / "report.md").resolve()

    for raw in ("logs/a.txt", "secrets.txt", "outputs/../secrets.txt"):
        with pytest.raises(ToolSandboxViolationError) as caught:
            sandbox.resolve_writable(root, raw)
        assert caught.value.details["allowed_subdirs"] == list(sandbox.WRITE_ALLOWED_SUBDIRS)


def test_writable_rejects_absolute_path(root: Path) -> None:
    """绝对路径即使落在沙箱内也拒（4.2.4 第 2 条：写入只接受相对路径）。"""
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.resolve_writable(root, str(root / "outputs" / "a.txt"))
    assert "Absolute paths are not writable" in caught.value.message

    with pytest.raises(ToolSandboxViolationError):
        sandbox.resolve_writable(root, TRAVERSAL)


def test_writable_allows_whitelisted_directory_outside_root(root: Path) -> None:
    """白名单目录不在沙箱根下 → 用相对路径 `../docs/...` 写入（`allowed_paths` 是运营显式放开的）。

    写入仍**必须是相对路径**（4.2.4 第 2 条），所以沙箱外的可写位置由 `allowed_paths`
    相对 `root` 给出（`_allowed_roots` 也是这么解析的）。
    """
    docs = root.parent / "docs"
    docs.mkdir()

    assert sandbox.resolve_writable(root, "../docs/a.md", allowed_paths=["../docs"]) == (docs / "a.md").resolve()
    with pytest.raises(ToolSandboxViolationError):
        sandbox.resolve_writable(root, "../docs/a.md")  # 未白名单 → 越界


# --------------------------------------------------------------------------------------
# 第 3 条：大小
# --------------------------------------------------------------------------------------
def test_size_gate_uses_byte_length() -> None:
    sandbox.ensure_size_within("中文", limit=6)  # 6 字节 == 上限，通过

    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_size_within("中文", limit=5, what="`content`")
    assert caught.value.details == {"size_bytes": 6, "limit_bytes": 5}

    sandbox.ensure_size_within(b"abc", limit=3)
    with pytest.raises(ToolSandboxViolationError):
        sandbox.ensure_size_within(b"abcd", limit=3)


def test_truncate_text_reports_and_keeps_valid_utf8() -> None:
    text, truncated = sandbox.truncate_text("hello", max_bytes=5)
    assert (text, truncated) == ("hello", False)

    clipped, truncated = sandbox.truncate_text("hello world", max_bytes=5)
    assert (clipped, truncated) == ("hello", True)

    multibyte, truncated = sandbox.truncate_text("你好", max_bytes=3)  # 3 字节只够一个字的前半
    assert truncated is True and multibyte == "你"


def test_display_path_prefers_relative_posix(root: Path, tmp_path: Path) -> None:
    assert sandbox.display_path(root, root / "outputs" / "a.txt") == "outputs/a.txt"
    assert sandbox.display_path(root, tmp_path / "elsewhere.txt") == (tmp_path / "elsewhere.txt").as_posix()


# --------------------------------------------------------------------------------------
# 第 4 条：网络（开关 + 白名单 + 禁回环 / 内网，防 SSRF）
# --------------------------------------------------------------------------------------
def test_network_requires_explicit_switch() -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_network_allowed("api.example.com", allow_network=False, allowed_hosts=["api.example.com"])
    assert caught.value.details["reason"] == "NETWORK_DISABLED"


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "10.1.2.3", "192.168.0.10", "172.16.0.1", "169.254.169.254", "0.0.0.0", "db.internal"],
)
def test_network_rejects_private_and_loopback_hosts(host: str) -> None:
    """内网 / 回环 / 链路本地（云元数据地址）一律拒 —— 即使被写进白名单。"""
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_network_allowed(host, allow_network=True, allowed_hosts=[host])
    assert caught.value.details["reason"] == "PRIVATE_HOST"


@pytest.mark.parametrize("host", ["", "   "])
def test_network_rejects_empty_host(host: str) -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_network_allowed(host, allow_network=True, allowed_hosts=["api.example.com"])
    assert caught.value.details["reason"] == "EMPTY_HOST"


def test_network_requires_non_empty_allowlist() -> None:
    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_network_allowed("api.example.com", allow_network=True, allowed_hosts=[])
    assert caught.value.details["allowed_hosts"] == []


def test_network_allowlist_matching_rules() -> None:
    """2.5：精确 host、`.example.com` 与 `*.example.com` 后缀形态；端口会被剥掉。"""
    sandbox.ensure_network_allowed("api.example.com", allow_network=True, allowed_hosts=["api.example.com"])
    sandbox.ensure_network_allowed("api.example.com:443", allow_network=True, allowed_hosts=["API.Example.com"])
    sandbox.ensure_network_allowed("a.example.com", allow_network=True, allowed_hosts=[".example.com"])
    sandbox.ensure_network_allowed("a.example.com", allow_network=True, allowed_hosts=["*.example.com"])
    sandbox.ensure_network_allowed("8.8.8.8", allow_network=True, allowed_hosts=["8.8.8.8"])

    with pytest.raises(ToolSandboxViolationError) as caught:
        sandbox.ensure_network_allowed("evil.example.com", allow_network=True, allowed_hosts=["api.example.com"])
    assert caught.value.details["allowed_hosts"] == ["api.example.com"]
    # `.example.com` 不匹配裸域本身（后缀规则要求有点号前缀）
    with pytest.raises(ToolSandboxViolationError):
        sandbox.ensure_network_allowed("example.com", allow_network=True, allowed_hosts=[".example.com"])
