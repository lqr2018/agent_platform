"""5 个内置工具（详细设计 2.5 / 4.2.4）。

`BUILTIN_TOOLS` 的顺序 = 迁移种子顺序 = 2.5 表格顺序（`calculator`、`file_read`、
`file_write`、`web_search`、`python_execute`）；稳定 id 由 `registry.BUILTIN_TOOL_IDS` 分配。
"""

from __future__ import annotations

from app.runtime.tools.base import BaseTool
from app.runtime.tools.builtin.calculator import CalculatorTool
from app.runtime.tools.builtin.file_read import FileReadTool
from app.runtime.tools.builtin.file_write import FileWriteTool
from app.runtime.tools.builtin.python_execute import PythonExecuteTool
from app.runtime.tools.builtin.web_search import WebSearchTool

BUILTIN_TOOLS: tuple[BaseTool, ...] = (
    CalculatorTool(),
    FileReadTool(),
    FileWriteTool(),
    WebSearchTool(),
    PythonExecuteTool(),
)
