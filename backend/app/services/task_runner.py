"""进程内后台任务队列（详细设计 4.6.2 / 1.5.4，SD-9：不引入 Celery / Redis）。

Phase 5 的摄取是"长任务"：上传立刻返回 `202`，进度靠 `documents.status` 轮询（4.6.2）。
实现是 `asyncio.Queue` + 固定并发 worker（默认 `TASK_RUNNER_CONCURRENCY=1`）。

三个工程点（直接来自 Phase 3 的 W1/W2 教训）：

1. **失败不静默**：worker 捕获 job 的异常并记日志，但**状态收敛由 job 自己负责**
   （`kb_service.ingest_document` 会把 `documents.status` 写成 `failed`）——队列层不碰业务状态，
   否则"谁该写 failed"会有两个答案；
2. **启动收敛**：进程崩溃时队列里的 job 不会再跑，`kb_service.converge_interrupted_documents()`
   在启动时把 in-flight（`parsing` / `chunking` / `embedding`）批量置 `failed` 并允许 `reingest`；
3. **关闭收敛**：`stop()` 先给 worker 发哨兵并等它们在 `drain_seconds` 内收尾，超时才取消任务
   （与 `workflow_service.shutdown_active_runs()` 同一思路）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.core.config import Settings, get_settings
from app.core.errors import InternalError
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DRAIN_SECONDS = 5.0
"""`stop()` 等待队列排空的上限（秒）；超时则取消，避免退出时挂死。"""

Job = Callable[[], Awaitable[object]]
"""job 的签名：无参协程；返回值（如 `ingest_document` 的终态字符串）会被忽略。"""


class TaskRunner:
    """单进程内的任务队列（`submit()` 立即返回，job 由 worker 顺序执行）。"""

    def __init__(
        self,
        *,
        concurrency: int = 1,
        name: str = "task-runner",
        drain_seconds: float = DEFAULT_DRAIN_SECONDS,
    ) -> None:
        self._name = name
        self._concurrency = max(1, concurrency)
        self._drain_seconds = drain_seconds
        self._queue: asyncio.Queue[Job | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = True

    @property
    def pending(self) -> int:
        """队列里还没被取走的 job 数（不含正在执行的）。"""
        return self._queue.qsize()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def is_running(self) -> bool:
        return any(not worker.done() for worker in self._workers)

    def start(self) -> None:
        """启动 worker（幂等；需要在事件循环里调用）。"""
        if self.is_running:
            return
        self._accepting = True
        loop = asyncio.get_running_loop()
        self._workers = [
            loop.create_task(self._worker(), name=f"{self._name}:{index}") for index in range(self._concurrency)
        ]

    async def submit(self, job: Job) -> None:
        """入队（懒启动 worker；已 `stop()` 后拒绝新任务）。"""
        if not self._accepting:
            raise InternalError("Task runner is shutting down and rejects new jobs")
        self.start()
        await self._queue.put(job)

    async def stop(self) -> int:
        """停止接收新任务并等队列排空；返回被**强制取消**的 worker 数（`drain_seconds` 超时）。"""
        self._accepting = False
        for _ in self._workers:
            await self._queue.put(None)
        canceled = 0
        if self._workers:
            done, pending = await asyncio.wait(self._workers, timeout=self._drain_seconds)
            canceled = len(pending)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info("task_runner.stopped", name=self._name, drained=len(done), canceled=canceled)
        self._workers = []
        return canceled

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            if job is None:
                return
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 状态收敛由 job 负责（见模块 docstring 第 1 条）；这里只保证 worker 不被打断
                logger.error("task_runner.job_failed", name=self._name, exc_info=True)


_task_runner: TaskRunner | None = None


def get_task_runner(settings: Settings | None = None, *, reset: bool = False) -> TaskRunner:
    """进程级单例（`TASK_RUNNER_CONCURRENCY` 决定 worker 数）。"""
    global _task_runner  # noqa: PLW0603 - 模块级单例
    if _task_runner is None or reset:
        resolved = settings or get_settings()
        _task_runner = TaskRunner(concurrency=resolved.task_runner_concurrency)
    return _task_runner


def reset_task_runner() -> None:
    """清掉单例（测试用；不负责 `stop()`，调用方应先停再重置）。"""
    global _task_runner  # noqa: PLW0603 - 模块级单例
    _task_runner = None
