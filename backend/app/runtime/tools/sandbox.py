"""沙箱限制（详细设计 4.2.4、2.5）。

四类限制（4.2.4 写死）：

1. 路径 —— `resolve()` 后必须位于 `SANDBOX_ROOT` 或 `allowed_paths` 之下（拒符号链接逃逸）；
2. 目录 —— `file_write` 只在 `uploads/`、`outputs/` 下写入，且拒绝绝对路径写入；
3. 大小 —— 读 ≤ 5 MB、写 ≤ 1 MB；工具输出按 `TOOL_MAX_OUTPUT_BYTES` 截断（4.2.3 步骤 8）；
4. 网络 —— 仅 `allowed_hosts`，并拒绝回环与内网地址（防 SSRF）。

`python_execute` 的 AST 白名单与子进程隔离在 `builtin/python_execute.py`（4.2.4 第 4 条）。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from pathlib import Path

from app.core.errors import ToolSandboxViolationError

READ_MAX_BYTES = 5 * 1024 * 1024
WRITE_MAX_BYTES = 1024 * 1024
WRITE_ALLOWED_SUBDIRS = ("uploads", "outputs")
PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost")


def resolve_readable(root: Path, raw: str, *, allowed_paths: Sequence[str] = ()) -> Path:
    """读取目标：解析符号链接后仍须落在沙箱根或白名单内（4.2.4 第 1 条）。"""
    candidate = _resolve(root, raw)
    if not _within(candidate, root, allowed_paths):
        raise ToolSandboxViolationError(
            f"Path '{raw}' is outside the sandbox",
            details={"path": raw, "sandbox_root": str(root), "allowed_paths": list(allowed_paths)},
        )
    return candidate


def resolve_writable(root: Path, raw: str, *, allowed_paths: Sequence[str] = ()) -> Path:
    """写入目标：在读取规则之上，额外拒绝绝对路径与非白名单子目录（4.2.4 第 2 条）。"""
    if Path(raw).is_absolute():
        raise ToolSandboxViolationError(f"Absolute paths are not writable: '{raw}'", details={"path": raw})
    candidate = _resolve(root, raw)
    if not _within(candidate, root, allowed_paths):
        raise ToolSandboxViolationError(
            f"Path '{raw}' is outside the sandbox",
            details={"path": raw, "sandbox_root": str(root), "allowed_paths": list(allowed_paths)},
        )
    if candidate.is_relative_to(root):
        relative = candidate.relative_to(root)
        if relative.parts and relative.parts[0] not in WRITE_ALLOWED_SUBDIRS:
            raise ToolSandboxViolationError(
                f"Writes are only allowed under {', '.join(WRITE_ALLOWED_SUBDIRS)}/",
                details={"path": raw, "allowed_subdirs": list(WRITE_ALLOWED_SUBDIRS)},
            )
    return candidate


def ensure_size_within(data: bytes | str, *, limit: int, what: str = "content") -> None:
    """写入前的大小闸门（4.2.4 第 3 条）。"""
    size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if size > limit:
        raise ToolSandboxViolationError(
            f"{what} exceeds the {limit} bytes limit",
            details={"size_bytes": size, "limit_bytes": limit},
        )


def ensure_network_allowed(host: str, *, allow_network: bool, allowed_hosts: Sequence[str]) -> None:
    """网络闸门（4.2.4 第 4 条）：需显式允许 + 命中白名单 + 非内网地址。"""
    if not allow_network:
        raise ToolSandboxViolationError(
            f"Network access is not allowed for this tool (host='{host}')",
            details={"host": host, "reason": "NETWORK_DISABLED"},
        )
    normalized = host.strip().lower().split(":")[0]
    if not normalized:
        raise ToolSandboxViolationError("Host must not be empty", details={"reason": "EMPTY_HOST"})
    if _is_private_host(normalized):
        raise ToolSandboxViolationError(
            f"Host '{host}' points to a private / loopback address",
            details={"host": host, "reason": "PRIVATE_HOST"},
        )
    if not allowed_hosts:
        raise ToolSandboxViolationError(
            f"Host '{host}' is not allowed (allowed_hosts is empty)",
            details={"host": host, "allowed_hosts": []},
        )
    if not any(_host_matches(normalized, item) for item in allowed_hosts):
        raise ToolSandboxViolationError(
            f"Host '{host}' is not in allowed_hosts",
            details={"host": host, "allowed_hosts": list(allowed_hosts)},
        )


def truncate_text(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """按字节截断（4.2.3 步骤 8）；返回 `(文本, 是否发生截断)`。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def display_path(root: Path, path: Path) -> str:
    """展示用路径：沙箱内用相对 POSIX 路径，否则用绝对路径（4.2.4 的工具返回值）。"""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _resolve(root: Path, raw: str) -> Path:
    if not raw or not raw.strip():
        raise ToolSandboxViolationError("Path must not be empty", details={"path": raw})
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _within(path: Path, root: Path, allowed_paths: Sequence[str]) -> bool:
    return any(path == base or path.is_relative_to(base) for base in (root, *_allowed_roots(root, allowed_paths)))


def _allowed_roots(root: Path, allowed_paths: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for item in allowed_paths:
        candidate = Path(item)
        roots.append((candidate if candidate.is_absolute() else root / candidate).resolve())
    return roots


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "0.0.0.0"} or host.endswith(PRIVATE_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _host_matches(host: str, pattern: str) -> bool:
    """白名单匹配：精确 host，或 `.example.com` / `*.example.com` 后缀形态（2.5）。"""
    normalized = pattern.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("*."):
        normalized = normalized[1:]
    if normalized.startswith("."):
        return host.endswith(normalized)
    return host == normalized
