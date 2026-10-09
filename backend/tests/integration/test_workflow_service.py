"""Workflow 服务层的"收尾加固"测试（详细设计 2.6 / 4.5.4 / 6.3）。

`test_workflow_runner.py` 走 HTTP 端到端；这里直接对服务层的**收尾函数**做契约测试 ——
它们守的是"取消 / 超时 / 进程退出之后，状态必须收敛"这条底线，并且必须能容忍
**已经失效的 session**（审计里真实穿透过 `PendingRollbackError`：取消打断引擎里的
`commit()` 之后，同一 session 上的任何操作都会失败，`workflow_runs` 于是永久停在 `running`）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus
from app.core.errors import RunCanceledError
from app.db.models import NodeRun, Run, WorkflowRun
from app.runtime.observability.tracer import Tracer
from app.services import trace_service, workflow_service


class _CommitFailsSession:
    """包装真实 session，只让 `commit()` 失败（模拟"落终态那一刻连接被掐断"）。

    其余方法原样转发 —— `_finalize_resilient` 因此能在**原 session 不可写**的前提下，
    走"换独立 session 重试"的兜底分支。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def commit(self) -> None:
        raise OperationalError("UPDATE workflow_runs ...", {}, sqlite3.OperationalError("disk I/O error"))


async def _create_running_rows(session: AsyncSession) -> tuple[WorkflowRun, Run]:
    """造一对 `running` 行（`WorkflowRun` + 它的 `runs` 行），与真实启动路径的落库形态一致。"""
    workflow_run = WorkflowRun(
        workflow_id="01J8Z000000000000000000WF1",
        workflow_version=1,
        definition_snapshot={},
        status=str(RunStatus.RUNNING),
        trigger="manual",
        input={},
        output={},
        state={},
        checkpoint={},
    )
    session.add(workflow_run)
    await session.commit()
    await session.refresh(workflow_run)

    api_run = Run(
        id="01J8Z000000000000000000AR9",
        kind="workflow",
        workflow_run_id=workflow_run.id,
        status=str(RunStatus.RUNNING),
        input={},
        output={},
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(api_run)
    await session.commit()
    await session.refresh(api_run)
    return workflow_run, api_run


async def _add_running_node_run(session: AsyncSession, workflow_run_id: str) -> NodeRun:
    """造一行停在 `running` 的 `node_runs`（引擎被取消时可能留下的窗口）。"""
    row = NodeRun(
        run_id=workflow_run_id,
        node_id="planner",
        node_type="agent",
        name="规划",
        seq=1,
        iteration=1,
        attempt=1,
        status=str(RunStatus.RUNNING),
        input={},
        output={},
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_finalize_resilient_recovers_invalidated_session(session: AsyncSession) -> None:
    """W1：session 已被取消打断（后续操作一律 `PendingRollbackError`）时仍要落终态。"""
    workflow_run, api_run = await _create_running_rows(session)
    workflow_run_id, api_run_id = workflow_run.id, api_run.id
    await _add_running_node_run(session, workflow_run_id)

    # 制造 "必须 rollback" 状态：一次失败的 flush（NOT NULL 违反）之后，session 拒绝任何后续操作
    # —— 与"取消打断 `commit()`"落到的是同一个状态（`PendingRollbackError`）
    session.add(
        WorkflowRun(
            workflow_id=None,
            workflow_version=1,
            definition_snapshot={},
            status=str(RunStatus.RUNNING),
            trigger="manual",
            input={},
            output={},
            state={},
            checkpoint={},
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    with pytest.raises(PendingRollbackError):
        await session.get(WorkflowRun, workflow_run_id)

    finalized = await workflow_service._finalize_resilient(
        session=session,
        workflow_run_id=workflow_run_id,
        api_run_id=api_run_id,
        status=RunStatus.CANCELED,
        result=None,
        error=RunCanceledError("Workflow run was canceled"),
        runner=None,
        run_trace=None,
        tracer=Tracer(),
        span_sink=trace_service.DatabaseSpanSink(session),
        emit=None,
    )

    assert finalized is True
    await session.rollback()
    stored_workflow_run = await session.get(WorkflowRun, workflow_run_id)
    assert stored_workflow_run is not None
    assert stored_workflow_run.status == str(RunStatus.CANCELED)
    assert stored_workflow_run.error_code == "RUN_CANCELED"
    assert stored_workflow_run.ended_at is not None

    stored_api_run = await session.get(Run, api_run_id)
    assert stored_api_run is not None
    assert stored_api_run.status == str(RunStatus.CANCELED)
    assert stored_api_run.canceled_at is not None  # W4：取消时间必须可查

    rows = (await session.execute(select(NodeRun).where(NodeRun.run_id == workflow_run_id))).scalars().all()
    assert [row.status for row in rows] == [str(RunStatus.CANCELED)]  # W3：节点行不留 running


async def test_finalize_resilient_retries_with_fresh_session(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1：原 session 写不进去时，用**独立 session** 再收敛一次（终态不能因为一次写失败而丢）。"""
    workflow_run, api_run = await _create_running_rows(session)
    workflow_run_id, api_run_id = workflow_run.id, api_run.id

    attempts: list[str] = []
    original_finalize = workflow_service._finalize

    async def counting_finalize(**kwargs: Any) -> None:
        attempts.append("attempt")
        await original_finalize(**kwargs)

    monkeypatch.setattr(workflow_service, "_finalize", counting_finalize)

    finalized = await workflow_service._finalize_resilient(
        session=_CommitFailsSession(session),
        workflow_run_id=workflow_run_id,
        api_run_id=api_run_id,
        status=RunStatus.FAILED,
        result=None,
        error=RunCanceledError("Workflow run was canceled"),
        runner=None,
        run_trace=None,
        tracer=Tracer(),
        span_sink=trace_service.DatabaseSpanSink(session),
        emit=None,
    )

    assert finalized is True
    assert attempts == ["attempt", "attempt"]  # 第一次写失败 → 换独立 session 重试一次
    await session.rollback()
    stored_workflow_run = await session.get(WorkflowRun, workflow_run_id)
    assert stored_workflow_run is not None
    assert stored_workflow_run.status == str(RunStatus.FAILED)
    stored_api_run = await session.get(Run, api_run_id)
    assert stored_api_run is not None
    assert stored_api_run.status == str(RunStatus.FAILED)


async def test_converge_canceled_run_is_idempotent(session: AsyncSession) -> None:
    """W2/W3 兜底：独立 session 的取消收敛要收掉节点行，并且对已终态 / 不存在的行都幂等。"""
    workflow_run, api_run = await _create_running_rows(session)
    workflow_run_id, api_run_id = workflow_run.id, api_run.id
    await _add_running_node_run(session, workflow_run_id)

    assert await workflow_service._converge_canceled_run(workflow_run_id, api_run_id, reason="shutdown") is True
    assert await workflow_service._converge_canceled_run(workflow_run_id, api_run_id, reason="shutdown") is True
    # 不存在的 id 不该抛（进程正在退出，收敛失败只能是日志）
    assert await workflow_service._converge_canceled_run("01J8Z00000000000000000GH0ST", "", reason="shutdown") is True

    await session.rollback()
    stored_workflow_run = await session.get(WorkflowRun, workflow_run_id)
    assert stored_workflow_run is not None
    assert stored_workflow_run.status == str(RunStatus.CANCELED)
    assert stored_workflow_run.error_code == "RUN_CANCELED"

    stored_api_run = await session.get(Run, api_run_id)
    assert stored_api_run is not None
    assert stored_api_run.canceled_at is not None

    rows = (await session.execute(select(NodeRun).where(NodeRun.run_id == workflow_run_id))).scalars().all()
    assert [row.status for row in rows] == [str(RunStatus.CANCELED)]
    assert rows[0].ended_at is not None
