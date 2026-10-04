"""工具服务（详细设计 4.2.2 / 4.2.3 步骤 8 / 3.2.3 / 2.5 / 6.3 第 2 条）。

服务层是 `tools` / `tool_invocations` 与 runtime 之间**唯一**的适配点（1.2：runtime 不碰 ORM）：

1. 读侧：`tools` 行 → `ToolDefinition` 快照（`to_definition`），供 `AgentRuntime` 每步重算可见工具；
2. 装配：`build_toolkit` 造 `ToolKit`（注册表 + 九步执行器 + 定义加载闭包），chat_service 一行接入；
3. 写侧：`DatabaseToolInvocationSink` 把 `ToolInvocationRecord` 落成 `tool_invocations` 一行（步骤 8）；
4. 启动对齐：`sync_builtin_tools` 把 5 个内置工具与 DB 对齐（4.2.2 / 6.3 第 2 条）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ToolNotFoundError
from app.core.logging import get_logger
from app.db.models.tool import Tool, ToolInvocation
from app.runtime.agent.runtime import DefinitionsLoader, ToolKit
from app.runtime.tools.base import ToolDefinition, ToolPermissionConfig
from app.runtime.tools.executor import ToolExecutor, ToolInvocationRecord, ToolInvocationSink
from app.runtime.tools.registry import ToolRegistry, default_registry, seed_rows

logger = get_logger(__name__)

TOOL_PAGE_LIMIT = 200
"""`GET /tools` 的返回上限（工具是配置类资源，量级很小；3.2.3 无分页参数）。"""

INVOCATION_PAGE_LIMIT = 200
"""`GET /tool-invocations` 的返回上限（审计视角，按 `created_at` 倒序）。"""

CODE_AUTHORITATIVE_FIELDS: tuple[str, ...] = (
    "display_name",
    "description",
    "input_schema",
    "output_schema",
    "builtin_name",
    "tool_type",
)
"""内置工具定义中**以代码为准**的列（4.2.2 的启动对齐只更新这些）。

`status` 与 `permission_config` 刻意不在其中：2.5 明确内置工具"可禁用 / 可改权限"，
运营侧的改动不能被重启抹掉。
"""


def to_definition(row: Tool) -> ToolDefinition:
    """`tools` 行 → runtime 快照（与 `runtime/tools/base.py` 的字段一一对应）。"""
    return ToolDefinition(
        id=row.id,
        name=row.name,
        display_name=row.display_name or row.name,
        description=row.description or "",
        tool_type=row.tool_type,
        status=row.status,
        input_schema=dict(row.input_schema or {}),
        output_schema=dict(row.output_schema or {}),
        builtin_name=row.builtin_name,
        http_config=dict(row.http_config or {}),
        permission_config=ToolPermissionConfig.from_mapping(row.permission_config),
        is_system=bool(row.is_system),
        tags=tuple(row.tags or ()),
    )


async def list_tools(
    session: AsyncSession,
    *,
    tool_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = TOOL_PAGE_LIMIT,
) -> Sequence[Tool]:
    """工具列表（3.2.3 的 `?tool_type=&status=&q=`；内置在前、按名称排序）。"""
    filters = []
    if tool_type:
        filters.append(Tool.tool_type == tool_type)
    if status:
        filters.append(Tool.status == status)
    if q:
        filters.append(Tool.name.contains(q) | Tool.display_name.contains(q))
    statement = select(Tool).where(*filters).order_by(Tool.is_system.desc(), Tool.name.asc()).limit(max(1, limit))
    return (await session.execute(statement)).scalars().all()


async def get_tool(session: AsyncSession, tool_id: str) -> Tool:
    """取工具；不存在 → `TOOL_NOT_FOUND`（404，附录 A）。"""
    row = await session.get(Tool, tool_id)
    if row is None:
        raise ToolNotFoundError(f"Tool '{tool_id}' does not exist", details={"tool_id": tool_id})
    return row


async def list_tool_invocations(
    session: AsyncSession,
    *,
    run_id: str | None = None,
    tool_id: str | None = None,
    limit: int = INVOCATION_PAGE_LIMIT,
) -> Sequence[ToolInvocation]:
    """调用明细（3.2.3 的 `?run_id=&tool_id=`；按 `created_at` 倒序，最新在前）。"""
    filters = []
    if run_id:
        filters.append(ToolInvocation.run_id == run_id)
    if tool_id:
        filters.append(ToolInvocation.tool_id == tool_id)
    statement = select(ToolInvocation).where(*filters).order_by(ToolInvocation.created_at.desc()).limit(max(1, limit))
    return (await session.execute(statement)).scalars().all()


def definition_loader(session: AsyncSession, tool_ids: Sequence[str]) -> DefinitionsLoader:
    """造"按步重查工具定义"的闭包（4.2.2：运行期禁用工具立即生效）。

    只查 Agent 引用的 id（`agents.tool_ids`），`status=enabled` 的过滤交给
    `ToolRegistry.visible_definitions`（纯函数，便于单测）。
    """
    ids = tuple(tool_ids)

    async def _load() -> list[ToolDefinition]:
        if not ids:
            return []
        rows = (await session.execute(select(Tool).where(Tool.id.in_(ids)))).scalars().all()
        return [to_definition(row) for row in rows]

    return _load


class DatabaseToolInvocationSink:
    """`tool_invocations` 的 ORM 写入（4.2.3 步骤 8）。

    与业务写入共用同一个 session（同 `trace_service.DatabaseSpanSink` 的理由：Phase 2 的执行
    是单任务串行的，共用连接行为最可预测）；`ToolExecutor` 已兜住写失败（不打断 Run）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write_invocation(self, record: ToolInvocationRecord) -> None:
        self._session.add(ToolInvocation(**record.as_row()))
        await self._session.commit()


def build_toolkit(
    session: AsyncSession,
    settings: Settings,
    *,
    tool_ids: Sequence[str],
    registry: ToolRegistry | None = None,
    sink: ToolInvocationSink | None = None,
    client_factory: Callable[[float], object] | None = None,
) -> ToolKit:
    """装配一次 Run 的 `ToolKit`（4.2.2 可见性 + 4.2.3 执行）。

    `registry` / `sink` / `client_factory` 是测试注入点（9.2：执行器与网络访问都可替身化）。
    """
    tool_registry = registry or default_registry()
    return ToolKit(
        registry=tool_registry,
        executor=ToolExecutor(
            registry=tool_registry,
            settings=settings,
            sink=sink or DatabaseToolInvocationSink(session),
            client_factory=client_factory,
        ),
        load_definitions=definition_loader(session, tool_ids),
    )


async def sync_builtin_tools(session: AsyncSession) -> dict[str, int]:
    """启动对齐（4.2.2 / 6.3 第 2 条）：把代码侧的 5 个内置工具同步进 `tools` 表。

    - 缺失 → 插入（`is_system=true`，`status` / 权限取代码默认）；
    - 已存在 → 只更新 `CODE_AUTHORITATIVE_FIELDS`（代码是内置工具定义的单一来源）；
    - 返回 `{"inserted", "updated", "unchanged"}` 计数供启动日志使用。
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for row in seed_rows():
        existing = await session.get(Tool, row["id"])
        if existing is None:
            session.add(Tool(**row))
            counts["inserted"] += 1
            continue
        if _apply_code_authoritative(existing, row):
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    await session.commit()
    if counts["inserted"] or counts["updated"]:
        logger.info("tools.builtin_synced", **counts)
    return counts


def _apply_code_authoritative(row: Tool, seed: dict[str, Any]) -> bool:
    """把代码侧字段写回 DB 行；返回是否发生了变化（用于启动日志计数）。"""
    changed = False
    for field in CODE_AUTHORITATIVE_FIELDS:
        value = seed[field]
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed
