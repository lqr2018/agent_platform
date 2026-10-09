"""应用装配：中间件、路由、异常处理器、lifespan（详细设计 1.1 / 7.1 任务 1–2、6.3）。

启动顺序（lifespan）：

1. `Settings.assert_production_secrets()` —— prod 下占位密钥直接失败（6.2）；
2. 准备运行时目录（`DATA_DIR` 下 app.db / chroma / files / uploads / reports）；
3. `verify_migrations_at_head()` —— 迁移必须等于 head，否则**直接退出**（6.3 第 1 条）；
4. 内置工具与 DB 对齐（6.3 第 2 条 / 4.2.2：代码是内置工具定义的单一来源）；
5. 孤儿 Run / WorkflowRun 收敛（6.3 第 3 条，Phase 3 接入）；
6. 生成 `features` 快照供 `/api/v1/meta`（6.3 第 5 条）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import APP_NAME, __version__
from app.api.v1.health import build_features, probe_router
from app.api.v1.router import api_router
from app.core import ids
from app.core.config import get_settings
from app.core.errors import (
    HTTP_STATUS_TO_CODE,
    AppError,
    ConflictError,
    DatabaseBusyError,
    ErrorCode,
    InternalError,
    ValidationError,
)
from app.core.logging import configure_logging, get_logger
from app.db import session as db_session
from app.runtime.observability import context as trace_context
from app.runtime.rag import reset_memory_vector_store
from app.schemas.common import ErrorResponse
from app.services import kb_service, run_service, tool_service, workflow_service
from app.services.task_runner import get_task_runner

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


class RequestIdMiddleware:
    """纯 ASGI 中间件（不用 `BaseHTTPMiddleware`，避免包装 SSE 流式响应）。

    1. 复用入站 `X-Request-Id`（便于跨服务串联），缺失则生成 32 位十六进制（0.2.1）；
    2. 绑定 ContextVar → 每条日志自动带上 `request_id`（1.5.1）；
    3. 响应头回写 `X-Request-Id`（7.1 任务 1）；
    4. 记录 `http.request` 访问日志（含 `status` / `duration_ms`）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER) or ids.new_request_id()
        # 兜底：ServerErrorMiddleware 在本中间件之外，500 时 ContextVar 已还原（见 schemas/common.py）
        scope.setdefault("state", {})["request_id"] = request_id
        token = trace_context.bind_request_id(request_id)
        started = perf_counter()
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message).setdefault(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            trace_context.reset_request_id(token)
            logger.info(
                "http.request",
                method=method,
                path=path,
                status=status_code,
                duration_ms=round((perf_counter() - started) * 1000, 3),
                request_id=request_id,  # 显式带上：DoD 要求与响应体 meta.request_id 一致
            )


# ---- 全局异常处理器（1.6 / 7.1 任务 2） ----
def _error_response(
    error: AppError,
    *,
    request: Request | None = None,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse.of(str(error.code), error.message, details=error.details, request=request)
    return JSONResponse(
        status_code=status_code or error.http_status,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def _format_validation_errors(raw_errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic 错误 → 字段级明细（`details.errors[]`，1.6）。"""
    return [
        {
            "loc": [str(part) for part in item.get("loc", [])],
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in raw_errors
    ]


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = exc if isinstance(exc, AppError) else InternalError()
    if error.http_status >= 500:
        logger.error("app_error", code=str(error.code), message=error.message, path=request.url.path)
    else:
        logger.info("app_error", code=str(error.code), message=error.message, path=request.url.path)
    return _error_response(error, request=request)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    raw = exc.errors() if isinstance(exc, RequestValidationError) else []
    details = {"errors": _format_validation_errors(list(raw))}
    return _error_response(ValidationError(details=details), request=request)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - 防御
        return _error_response(InternalError(), request=request)
    error = AppError(str(exc.detail) if exc.detail else f"HTTP {exc.status_code}")
    error.code = HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return _error_response(error, request=request, status_code=exc.status_code)


async def _integrity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """1.6：唯一约束 → `CONFLICT`(409)；外键 → 422。"""
    raw = str(getattr(exc, "orig", exc))
    logger.warning("db.integrity_error", path=request.url.path, detail=raw)
    upper = raw.upper()
    if "FOREIGN KEY" in upper:
        message = "Foreign key constraint violated"
        return _error_response(ValidationError(message, details={"reason": raw}), request=request)
    return _error_response(ConflictError("Database constraint violated", details={"reason": raw}), request=request)


def _operational_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """SQLite 写锁竞争 → 503（明确"可以重试"）；其它 `OperationalError` 仍按 500 处理。

    不加这个处理器时锁冲突会走 `Exception` 兜底，客户端只看到 500 —— 既误导（像是代码 bug）
    又不利于前端做退避重试。判断依据是驱动原文，避免把真正的实现缺陷伪装成"重试就好"。
    """
    raw = str(getattr(exc, "orig", exc))
    logger.warning("db.operational_error", path=request.url.path, detail=raw)
    lowered = raw.lower()
    if "locked" in lowered or "busy" in lowered:
        return _error_response(DatabaseBusyError(details={"reason": raw}), request=request)
    return _error_response(InternalError(), request=request)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常：堆栈只进日志，响应体只回 `INTERNAL_ERROR` + `meta.request_id`（1.6）。"""
    error = InternalError()
    body = ErrorResponse.of(str(error.code), error.message, request=request)
    logger.error(
        "unhandled_exception",
        error_type=type(exc).__name__,
        message=str(exc),
        path=request.url.path,
        # 本处理器在 RequestIdMiddleware 之外运行，ContextVar 已还原 → 显式带上 request_id
        request_id=body.meta.request_id,
        exc_info=exc,
    )
    headers = {REQUEST_ID_HEADER: body.meta.request_id} if body.meta.request_id else None
    return JSONResponse(
        status_code=error.http_status,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def _register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(IntegrityError, _integrity_error_handler)
    app.add_exception_handler(OperationalError, _operational_error_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


# ---- lifespan（6.3） ----
async def _sync_builtin_tools() -> None:
    """6.3 第 2 条：把 `runtime/tools/builtin/` 的定义与 `tools` 表对齐（4.2.2）。

    缺失则插入、`description` / `input_schema` 等以代码为准更新；运营侧改过的
    `status` / `permission_config` 不动（2.5：内置工具可禁用、可改权限）。
    """
    async with db_session.get_sessionmaker()() as session:
        counts = await tool_service.sync_builtin_tools(session)
    logger.info("startup.builtin_tools_ready", **counts)


async def _converge_orphan_runs() -> None:
    """6.3 第 3 条：把上次进程留下的 `running`（超 `timeout × 2`）标为 `failed`。

    失败只记日志、不阻断启动（6.3 的开头约定：除第 1 条外都不阻断）。
    """
    try:
        async with db_session.get_sessionmaker()() as session:
            counts = await workflow_service.converge_orphan_runs(session, get_settings())
    except Exception:
        logger.warning("startup.orphan_runs_failed", exc_info=True)
        return
    if counts["workflow_runs"] or counts["runs"]:
        logger.info("startup.orphan_runs_converged", **counts)


async def _converge_interrupted_documents() -> None:
    """Phase 5：把上次进程留下的 `parsing/chunking/embedding` 文档收敛为 `failed`。

    与 `_converge_orphan_runs` 同一思路（6.3 第 3 条）：进程重启后队列里的摄取任务不会再跑，
    不收敛的话文档会永远停在中间态（4.6.2 + `services/task_runner.py` 的"启动收敛"说明）。
    失败只记日志、不阻断启动。
    """
    try:
        async with db_session.get_sessionmaker()() as session:
            count = await kb_service.converge_interrupted_documents(session)
    except Exception:
        logger.warning("startup.interrupted_documents_failed", exc_info=True)
        return
    if count:
        logger.info("startup.interrupted_documents_converged", count=count)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.assert_production_secrets()
    for name, path in settings.runtime_dirs().items():
        path.mkdir(parents=True, exist_ok=True)
        logger.debug("startup.dir_ready", name=name, path=str(path))
    app.state.settings = settings
    app.state.features = build_features(settings)
    # Phase 5：每次启动重建进程内单例（摄取队列与 memory 向量库都不跨"应用实例"复用；
    # 测试里多个 app 实例共用一个进程，重建才能保证互不污染）
    get_task_runner(settings, reset=True)
    reset_memory_vector_store()
    await db_session.verify_migrations_at_head()
    await _sync_builtin_tools()
    await _converge_orphan_runs()
    await _converge_interrupted_documents()
    logger.info(
        "app.startup",
        name=APP_NAME,
        version=__version__,
        app_env=settings.app_env,
        database_url=settings.database_url,
    )
    try:
        yield
    finally:
        # W2：收尾顺序不能颠倒 —— 先把仍在跑的后台 Run 任务取消并等它们写完终态（此时连接池还在），
        # 再关池。否则任务会停在 `running`，或在池关闭后写库直接报错。
        canceled_runs = await workflow_service.shutdown_active_runs()
        if canceled_runs:
            logger.info("app.shutdown_runs_converged", canceled=canceled_runs)
        # Phase 5：停摄取队列（等当前 job 收尾/取消），再关连接池 —— 顺序与上一行同理
        canceled_ingestions = await get_task_runner().stop()
        if canceled_ingestions:
            logger.info("app.shutdown_ingestions_converged", canceled=canceled_ingestions)
        run_service.reset_run_registry()
        await db_session.dispose_engine()
        logger.info("app.shutdown")


def create_app() -> FastAPI:
    """构造 FastAPI 应用（uvicorn 入口为模块级 `app`）。"""
    settings = get_settings()
    configure_logging(settings)
    application = FastAPI(
        title="Agent Platform API",
        description="配置驱动的 LLM Agent 编排与执行平台（详细设计 3.x）",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        redoc_url=None,
        default_response_class=JSONResponse,
    )
    application.add_middleware(RequestIdMiddleware)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER],
        )
    _register_exception_handlers(application)
    application.include_router(probe_router)  # /healthz、/readyz（根路径，3.1）
    application.include_router(api_router, prefix="/api/v1")
    return application


app = create_app()
