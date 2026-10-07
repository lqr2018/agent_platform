"""Run 生命周期与活跃 Run 注册表（详细设计 2.6 / 4.4.3 / 1.5.4）。

- `runs` 行在 Run 开始时插入（`status=running`），崩溃后能查到"孤儿 Run"（2.6）；
- **单会话最多 1 个活跃 Run**：并发请求返回 `CONFLICT`（1.5.4）；
- 全局并发闸门 `asyncio.Semaphore(MAX_CONCURRENT_RUNS)`：超出时排队而不是报错（1.5.4）；
- `cancel` 只对**本进程内活跃**的 Run 生效（置 `asyncio.Event`）；其它情况按 2.6 直接落终态。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.config import Settings, get_settings
from app.core.enums import RunKind, RunStatus
from app.core.errors import ConflictError, RunAlreadyFinishedError, RunNotFoundError
from app.core.logging import get_logger
from app.db.models import Run
from app.runtime.agent.state import RunResult

logger = get_logger(__name__)

TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED})


@dataclass(slots=True)
class ActiveRun:
    """进程内活跃 Run 的登记项（并发闸门 + 取消信号的落点）。"""

    run_id: str
    conversation_id: str | None
    cancel_event: asyncio.Event


class RunRegistry:
    """活跃 Run 注册表（1.5.4 / 1.5.5 的取消入口）。"""

    def __init__(self, *, max_concurrent: int = 4) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._active: dict[str, ActiveRun] = {}
        self._by_conversation: dict[str, str] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._acquired = 0

    # ---- 并发闸门 ----
    def _gate(self) -> asyncio.Semaphore:
        """懒创建：保证 Semaphore 绑定到当前事件循环（测试会换 loop）。"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        return self._semaphore

    async def acquire(self) -> None:
        """排队获取全局并发额度（1.5.4：超出则等待，不报错）。"""
        await self._gate().acquire()
        self._acquired += 1

    def release(self) -> None:
        """归还额度（多次调用安全）。"""
        if self._acquired <= 0:
            return
        self._acquired -= 1
        self._gate().release()

    # ---- 活跃登记 ----
    def begin(self, *, run_id: str, conversation_id: str | None) -> asyncio.Event:
        """登记一个活跃 Run；同一会话已有活跃 Run → `CONFLICT`（1.5.4）。"""
        if conversation_id and conversation_id in self._by_conversation:
            raise ConflictError(
                "This conversation already has an active run",
                details={"conversation_id": conversation_id, "run_id": self._by_conversation[conversation_id]},
            )
        cancel_event = asyncio.Event()
        self._active[run_id] = ActiveRun(run_id=run_id, conversation_id=conversation_id, cancel_event=cancel_event)
        if conversation_id:
            self._by_conversation[conversation_id] = run_id
        return cancel_event

    def finish(self, run_id: str) -> None:
        entry = self._active.pop(run_id, None)
        if entry and entry.conversation_id:
            self._by_conversation.pop(entry.conversation_id, None)

    def request_cancel(self, run_id: str) -> bool:
        """请求取消；返回是否为**本进程活跃**的 Run。"""
        entry = self._active.get(run_id)
        if entry is None:
            return False
        entry.cancel_event.set()
        return True

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    def clear(self) -> None:
        """测试用：清空登记表并重建闸门。"""
        self._active.clear()
        self._by_conversation.clear()
        self._semaphore = None


_registry: RunRegistry | None = None
_registry_size: int | None = None


def get_run_registry(settings: Settings | None = None) -> RunRegistry:
    """进程内单例（`MAX_CONCURRENT_RUNS` 变化时重建，便于测试）。"""
    global _registry, _registry_size  # noqa: PLW0603 - 模块级单例
    config = settings or get_settings()
    if _registry is None or _registry_size != config.max_concurrent_runs:
        _registry = RunRegistry(max_concurrent=config.max_concurrent_runs)
        _registry_size = config.max_concurrent_runs
    return _registry


def reset_run_registry() -> None:
    """测试与 lifespan 收尾调用。"""
    global _registry, _registry_size  # noqa: PLW0603 - 模块级单例
    _registry = None
    _registry_size = None


# ---- Run CRUD ----
async def create_run(
    session: AsyncSession,
    *,
    agent_id: str | None,
    conversation_id: str | None,
    kind: RunKind = RunKind.CHAT,
    trace_id: str,
    user_message_id: str,
    text: str,
    run_id: str | None = None,
) -> Run:
    """插入 `status=running` 的 Run 行（2.6：崩溃后能查到"孤儿 Run"）。"""
    run = Run(
        id=run_id or ids.new_ulid(),
        kind=str(kind),
        agent_id=agent_id,
        conversation_id=conversation_id,
        status=str(RunStatus.RUNNING),
        input={"user_message_id": user_message_id, "text": text},
        output={},
        trace_id=trace_id,
        started_at=_utcnow(),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_run(session: AsyncSession, run_id: str) -> Run:
    """取 Run；不存在 → `RUN_NOT_FOUND`（404）。"""
    run = await session.get(Run, run_id)
    if run is None:
        raise RunNotFoundError(f"Run '{run_id}' does not exist")
    return run


async def list_runs(
    session: AsyncSession,
    *,
    kind: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Run], int]:
    """运行列表（3.2.4：`?kind=&agent_id=&status=` + 偏移分页）。"""
    filters = []
    if kind:
        filters.append(Run.kind == kind)
    if agent_id:
        filters.append(Run.agent_id == agent_id)
    if conversation_id:
        filters.append(Run.conversation_id == conversation_id)
    if status:
        filters.append(Run.status == status)

    total = (await session.execute(select(func.count(Run.id)).where(*filters))).scalar_one()
    statement = (
        select(Run)
        .where(*filters)
        .order_by(Run.started_at.desc())
        .offset(max(0, page - 1) * page_size)
        .limit(page_size)
    )
    return (await session.execute(statement)).scalars().all(), int(total)


async def finish_run(
    session: AsyncSession,
    run: Run,
    result: RunResult,
    *,
    output_message_id: str | None = None,
) -> Run:
    """按 `RunResult` 落终态（2.6：进入终态后禁止再次变更）。"""
    if run.status in TERMINAL_STATUSES:
        raise RunAlreadyFinishedError(
            f"Run '{run.id}' is already {run.status}", details={"run_id": run.id, "status": run.status}
        )
    ended_at = _utcnow()
    run.status = str(result.status)
    run.output = {
        "message_id": output_message_id,
        "text": result.output_text,
        "finish_reason": result.finish_reason,
    }
    run.steps = result.steps
    run.tool_call_count = result.tool_call_count
    run.prompt_tokens = result.usage.prompt_tokens
    run.completion_tokens = result.usage.completion_tokens
    run.total_tokens = result.usage.total_tokens
    run.total_cost_usd = result.cost_usd
    run.ended_at = ended_at
    run.latency_ms = result.latency_ms
    run.error_code = result.error_code
    run.error_message = result.error_message
    if result.status == RunStatus.CANCELED:
        run.canceled_at = ended_at
    await session.commit()
    await session.refresh(run)
    return run


async def cancel_run(session: AsyncSession, run_id: str, *, registry: RunRegistry | None = None) -> Run:
    """取消（3.2.4）：活跃 Run 只置取消信号（由运行中的任务收敛终态）；孤儿 Run 直接落 `canceled`。

    `kind=workflow` 的行由**路由层**转交 `workflow_service.cancel_run`（见 `api/v1/runs.py`），
    避免 `services` 内部循环依赖（1.2）。
    """
    run = await get_run(session, run_id)
    if run.status in TERMINAL_STATUSES:
        raise RunAlreadyFinishedError(
            f"Run '{run_id}' is already {run.status}", details={"run_id": run_id, "status": run.status}
        )
    active = (registry or get_run_registry()).request_cancel(run_id)
    if not active:
        now = _utcnow()
        run.status = str(RunStatus.CANCELED)
        run.error_code = "RUN_CANCELED"
        run.error_message = "Run was canceled before it finished"
        run.canceled_at = now
        run.ended_at = now
        await session.commit()
        await session.refresh(run)
    return run


def _utcnow() -> datetime:
    """naive UTC（与 `db/base.now_utc` 一致，0.2.1）。"""
    return datetime.now(UTC).replace(tzinfo=None)
