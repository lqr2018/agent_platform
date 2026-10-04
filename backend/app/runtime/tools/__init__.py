"""Tool Runtime（详细设计 4.2 / 7.3）。

五个组成件（7.3 交付物，**不多建文件**）：

    base.py        工具契约（BaseTool / ToolContext / ToolResult / ToolDefinition）与 JSON Schema 子集校验
    registry.py    内存注册表：内置工具登记 + LLM function schema 生成 + 可见性规则（4.2.2）
    permissions.py 权限合并与判定（4.2.3 步骤 2/4/5）
    sandbox.py     路径 / 大小 / 网络限制（4.2.4）
    executor.py    九步执行流水线（4.2.3）

`runtime/**` 不 import `db/models` / `services`（1.2）：`tools` 行由服务层装配成
`ToolDefinition` 快照传进来（同 `llm/base.py` 的 `ProviderConfig` 手法）。
"""

from __future__ import annotations

from app.runtime.tools.base import (
    BaseTool,
    ToolContext,
    ToolDefinition,
    ToolInvocationResult,
    ToolPermissionConfig,
    ToolResult,
    normalize_arguments,
    validate_arguments,
)

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolDefinition",
    "ToolInvocationResult",
    "ToolPermissionConfig",
    "ToolResult",
    "normalize_arguments",
    "validate_arguments",
]
