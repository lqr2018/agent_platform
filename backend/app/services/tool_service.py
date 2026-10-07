"""工具服务（详细设计 4.2.2 / 4.2.3 步骤 8 / 3.2.3 / 2.5 / 6.3 第 2 条）。

服务层是 `tools` / `tool_invocations` 与 runtime 之间**唯一**的适配点（1.2：runtime 不碰 ORM）：

1. 读侧：`tools` 行 → `ToolDefinition` 快照（`to_definition`），供 `AgentRuntime` 每步重算可见工具；
2. 装配：`build_toolkit` 造 `ToolKit`（注册表 + 九步执行器 + 定义加载闭包），chat_service 一行接入；
3. 写侧：`DatabaseToolInvocationSink` 把 `ToolInvocationRecord` 落成 `tool_invocations` 一行（步骤 8）；
4. 启动对齐：`sync_builtin_tools` 把 5 个内置工具与 DB 对齐（4.2.2 / 6.3 第 2 条）；
5. 运营侧 CRUD 与试跑（3.2.3 的 `POST` / `PATCH` / `DELETE` / `POST /{id}/test`）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings
from app.core.enums import RunKind, SpanStatus, ToolType
from app.core.errors import ConflictError, ToolNotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.tool import Tool, ToolInvocation
from app.runtime.agent.emitter import NullEmitter
from app.runtime.agent.runtime import DefinitionsLoader, ToolKit
from app.runtime.llm.base import ToolCallSpec
from app.runtime.observability.tracer import Tracer
from app.runtime.tools.base import ToolDefinition, ToolInvocationResult, ToolPermissionConfig
from app.runtime.tools.executor import (
    NullInvocationSink,
    ToolExecutor,
    ToolInvocationRecord,
    ToolInvocationSink,
    ToolRunContext,
)
from app.runtime.tools.registry import ToolRegistry, default_registry, seed_rows
from app.schemas.tools import HTTP_METHODS, ToolCreate, ToolUpdate

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


# --------------------------------------------------------------------------------------
# 运营侧写端点（3.2.3）：POST / PATCH / DELETE / POST /{id}/test
# --------------------------------------------------------------------------------------
BUILTIN_EDITABLE_FIELDS: frozenset[str] = frozenset({"status", "permission_config", "tags"})
"""内置行允许 `PATCH` 的列。

2.5 明确内置工具"禁删、**可禁用 / 可改权限**"；其余列（`name` / `description` /
`input_schema` / `http_config` …）是**代码定义的属地**（4.2.2 的单一来源），
即便改成功也会被下一次启动对齐覆盖，因此直接 422 拒绝而不是静默失效。
"""


async def _ensure_name_available(session: AsyncSession, name: str) -> None:
    """`tools.name` 有 UNIQUE 约束（也是"内置与 `api` 工具不重名"的约束）→ 冲突给 409。"""
    exists = (await session.execute(select(Tool.id).where(Tool.name == name))).first()
    if exists:
        raise ConflictError(f"Tool name '{name}' already exists", details={"name": name})


def _normalize_http_config(config: Mapping[str, Any] | None, *, tool_type: str) -> dict[str, Any]:
    """`api` 工具的 `http_config` 闸门（2.5 的结构）：`url` 必填、`method` 收口到大写白名单。

    在**配置期**拦下"建了也跑不起来"的工具，比运行期才报 `TOOL_INVALID_ARGUMENTS` 更友好。
    """
    normalized = dict(config or {})
    if tool_type != ToolType.API:
        return normalized

    url = str(normalized.get("url") or "").strip()
    if not url:
        raise ValidationError(
            "`api` tools require a non-empty http_config.url",
            details={"field": "http_config.url", "tool_type": tool_type},
        )
    method = str(normalized.get("method") or "GET").strip().upper()
    if method not in HTTP_METHODS:
        raise ValidationError(
            f"Unsupported http_config.method '{method}'",
            details={"field": "http_config.method", "allowed": list(HTTP_METHODS)},
        )
    normalized["url"] = url
    normalized["method"] = method
    return normalized


async def create_tool(session: AsyncSession, data: ToolCreate) -> Tool:
    """`POST /tools`：建一个 `api` 工具（`builtin` 由迁移 / 启动对齐维护，`mcp` 属 Backlog）。"""
    await _ensure_name_available(session, data.name)
    tool = Tool(
        name=data.name,
        display_name=data.display_name or data.name,
        description=data.description,
        tool_type=str(ToolType.API),
        input_schema=dict(data.input_schema),
        output_schema=dict(data.output_schema),
        builtin_name=None,
        http_config=_normalize_http_config(data.http_config, tool_type=str(ToolType.API)),
        permission_config=data.permission_config.model_dump(mode="json"),
        status=data.status,
        tags=list(data.tags),
        is_system=False,
    )
    session.add(tool)
    await session.commit()
    await session.refresh(tool)
    return tool


async def update_tool(session: AsyncSession, tool_id: str, data: ToolUpdate) -> Tool:
    """`PATCH /tools/{id}`：内置行只放行 `status` / `permission_config` / `tags`（2.5）。"""
    tool = await get_tool(session, tool_id)
    changes = data.model_dump(exclude_unset=True)

    if tool.is_system:
        offenders = sorted(set(changes) - BUILTIN_EDITABLE_FIELDS)
        if offenders:
            raise ValidationError(
                "Builtin tool definitions are owned by code (4.2.2)",
                details={
                    "tool_id": tool_id,
                    "fields": offenders,
                    "editable": sorted(BUILTIN_EDITABLE_FIELDS),
                    "reason": "BUILTIN_DEFINITION_IS_CODE_OWNED",
                },
            )

    name = changes.get("name")
    if name is not None and name != tool.name:
        await _ensure_name_available(session, str(name))

    if "http_config" in changes:
        changes["http_config"] = _normalize_http_config(
            changes["http_config"], tool_type=str(tool.tool_type or ToolType.API)
        )

    for field, value in changes.items():
        setattr(tool, field, value)

    await session.commit()
    await session.refresh(tool)
    return tool


async def delete_tool(session: AsyncSession, tool_id: str) -> None:
    """`DELETE /tools/{id}`：内置行禁删（2.5，409）；删 `api` 工具时把历史调用的 `tool_id` 置空。

    `tool_invocations.tool_id` 在 2.5 里声明为 `ON DELETE SET NULL`（删工具不能连带删审计），
    这里显式置空是为了**不依赖 SQLite 的外键开关**（默认关闭，`PRAGMA foreign_keys=ON` 才生效）。
    """
    tool = await get_tool(session, tool_id)
    if tool.is_system:
        raise ConflictError(
            f"Builtin tool '{tool.name}' cannot be deleted",
            details={"tool_id": tool_id, "reason": "BUILTIN_TOOL_NOT_DELETABLE"},
        )
    await session.execute(update(ToolInvocation).where(ToolInvocation.tool_id == tool_id).values(tool_id=None))
    await session.delete(tool)
    await session.commit()


async def run_tool_test(
    session: AsyncSession,
    tool_id: str,
    *,
    settings: Settings,
    arguments: Mapping[str, Any] | None = None,
) -> tuple[Tool, ToolInvocationResult]:
    """`POST /tools/{id}/test`：走**同一套九步流水线**直接执行一次（3.2.3：跳过 LLM）。

    三个刻意的选择：

    - 用真实 `ToolExecutor` 而不是直接 `tool.run()` —— 参数校验 / 权限闸门 / 沙箱 / 超时 /
      输出截断全部生效，"试跑"结果才代表运行期行为（DoD 2 的"权限接口生效"就是这么验的）；
    - `NullInvocationSink` —— 试跑没有 `run_id`（`tool_invocations.run_id` 是指向 `runs` 的
      非空外键），因此**不落库**；试跑留痕靠日志与返回值；
    - `api` 工具的 host 白名单仍取自 `tools.permission_config`（沙箱网络闸门照常生效）。
    """
    row = await get_tool(session, tool_id)
    definition = to_definition(row)
    tracer = Tracer.from_settings(settings)
    run_trace = tracer.start_run(kind=RunKind.CHAT, name=f"tool-test:{row.name}", run_id=ids.new_ulid())
    executor = ToolExecutor(registry=default_registry(), settings=settings, sink=NullInvocationSink())
    try:
        result = await executor.execute(
            ToolCallSpec(id="tool-test", name=row.name, arguments=dict(arguments or {})),
            ctx=ToolRunContext(
                run_id=run_trace.run_id,
                definitions={row.name: definition},
                settings=settings,
                tracer=tracer,
                emitter=NullEmitter(),
                cancellation=asyncio.Event(),
                sandbox_root=settings.sandbox_path,
            ),
        )
    finally:
        await tracer.end_run(run_trace, status=SpanStatus.OK)
    return row, result
