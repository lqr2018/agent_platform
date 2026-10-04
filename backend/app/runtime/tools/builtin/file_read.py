"""`file_read` 内置工具（详细设计 2.5 / 4.2.4 第 1、3 条）。

- 路径必须落在 `SANDBOX_ROOT`（或 `allowed_paths`）内，且在 `resolve()` **之后**校验，
  以挡住 `../` 与符号链接逃逸；
- 单文件读取上限 `READ_MAX_BYTES`（5 MB）；输出按 `max_output_bytes` 截断（4.2.3 步骤 8）；
- MVP 只读 UTF-8 文本，二进制直接拒绝（范围决策见 2.5 的说明）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.core.enums import PermissionLevel
from app.core.errors import ToolExecutionFailedError, ToolInvalidArgumentsError
from app.runtime.tools import sandbox
from app.runtime.tools.base import BaseTool, ToolContext, ToolPermissionConfig, ToolResult


class FileReadTool(BaseTool):
    """4.2.4 的 `file_read`。"""

    name = "file_read"
    display_name = "文件读取"
    description = (
        "读取沙箱目录内的 UTF-8 文本文件。`path` 必须是相对沙箱根目录的相对路径，"
        "例如 `uploads/notes.txt`；越出沙箱或非文本文件会被拒绝。"
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对沙箱根目录的文件路径，例如 uploads/notes.txt",
                "minLength": 1,
                "maxLength": 500,
            },
            "max_bytes": {
                "type": "integer",
                "description": "最多返回的字节数（默认取平台配置 TOOL_MAX_OUTPUT_BYTES）",
                "minimum": 1,
                "maximum": sandbox.READ_MAX_BYTES,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
    }
    default_permission = ToolPermissionConfig(
        level=PermissionLevel.SAFE,  # 2.5 内置工具表：file_read = safe / false（路径限制由沙箱保证）
        timeout_seconds=15.0,
        max_calls_per_run=30,
    )

    async def run(self, ctx: ToolContext, *, path: str = "", max_bytes: int | None = None, **_: Any) -> ToolResult:
        target = sandbox.resolve_readable(ctx.sandbox_root, path, allowed_paths=ctx.allowed_paths)
        if not target.is_file():
            raise ToolExecutionFailedError(f"File not found: {path}", details={"path": path})

        size = target.stat().st_size
        if size > sandbox.READ_MAX_BYTES:
            raise ToolExecutionFailedError(
                f"File is too large to read ({size} bytes > {sandbox.READ_MAX_BYTES} bytes)",
                details={"path": path, "size_bytes": size},
            )

        data = await asyncio.to_thread(target.read_bytes)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionFailedError("File is not valid UTF-8 text", details={"path": path}) from exc

        limit = ctx.max_output_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise ToolInvalidArgumentsError("`max_bytes` must be a positive integer", details={"max_bytes": max_bytes})
        content, truncated = sandbox.truncate_text(text, max_bytes=min(limit, ctx.max_output_bytes))
        relative = sandbox.display_path(ctx.sandbox_root, target)
        return ToolResult(
            content=content,
            meta={"path": relative, "size_bytes": size, "truncated": truncated},
        )
