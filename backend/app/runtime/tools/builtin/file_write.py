"""`file_write` 内置工具（详细设计 2.5 / 4.2.4 第 1、2、3 条 + SD-17）。

- 写入位置仅限沙箱根下的 `uploads/`、`outputs/`（4.2.4 第 2 条），拒绝绝对路径与 `../`；
- 单次写入 ≤ `WRITE_MAX_BYTES`（1 MB）；`mode=append` 追加、`mode=overwrite` 覆盖（2.5）；
- `require_approval=true` + `level=guarded`：MVP 无审批通道，等价于
  "**未开启 `FILE_WRITE_ENABLED` 即 `TOOL_PERMISSION_DENIED`**"（SD-17）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.enums import PermissionLevel
from app.core.errors import ToolInvalidArgumentsError
from app.runtime.tools import sandbox
from app.runtime.tools.base import BaseTool, ToolContext, ToolPermissionConfig, ToolResult

WRITE_MODES = ("append", "overwrite")


class FileWriteTool(BaseTool):
    """4.2.4 的 `file_write`。"""

    name = "file_write"
    display_name = "文件写入"
    description = (
        "把文本写入沙箱目录下的 `uploads/` 或 `outputs/` 子目录。`mode=append` 追加、"
        "`mode=overwrite` 覆盖；单次写入上限 1 MB。若平台未开启该工具，调用会被拒绝。"
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对沙箱根目录的写入路径，必须以 uploads/ 或 outputs/ 开头",
                "minLength": 1,
                "maxLength": 500,
            },
            "content": {"type": "string", "description": "要写入的文本内容", "maxLength": sandbox.WRITE_MAX_BYTES},
            "mode": {
                "type": "string",
                "description": "append 追加（默认）或 overwrite 覆盖",
                "enum": list(WRITE_MODES),
                "default": "append",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    output_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "mode": {"type": "string"},
            "bytes_written": {"type": "integer"},
        },
    }
    default_permission = ToolPermissionConfig(
        level=PermissionLevel.GUARDED,
        require_approval=True,
        timeout_seconds=15.0,
        max_calls_per_run=10,
    )

    async def run(
        self, ctx: ToolContext, *, path: str = "", content: str = "", mode: str = "append", **_: Any
    ) -> ToolResult:
        if mode not in WRITE_MODES:
            raise ToolInvalidArgumentsError(f"`mode` must be one of {list(WRITE_MODES)}", details={"mode": mode})
        sandbox.ensure_size_within(content, limit=sandbox.WRITE_MAX_BYTES, what="`content`")
        target = sandbox.resolve_writable(ctx.sandbox_root, path, allowed_paths=ctx.allowed_paths)

        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        if mode == "append":
            await asyncio.to_thread(_append_text, target, content)
        else:
            await asyncio.to_thread(target.write_text, content, encoding="utf-8")

        written = len(content.encode("utf-8"))
        relative = sandbox.display_path(ctx.sandbox_root, target)
        return ToolResult(
            content=f"Wrote {written} bytes to {relative} (mode={mode})",
            meta={"path": relative, "mode": mode, "bytes_written": written},
        )


def _append_text(path: Path, text: str) -> None:
    """追加写入（`to_thread` 里调用，避免阻塞事件循环）。"""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
