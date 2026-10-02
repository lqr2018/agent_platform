"""基础设施端点（详细设计 3.2.1 / 6.3）。

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/healthz` | 进程存活（不查依赖） |
| GET | `/readyz` | 就绪探针：DB 可写 + 迁移到 head + 向量库可达；失败 503 |
| GET | `/api/v1/meta` | 版本 + 能力开关快照（前端据此隐藏 Backlog 菜单，5.2） |
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app import APP_NAME, __version__
from app.core.config import Settings, get_settings
from app.db import session as db_session
from app.schemas.common import ApiResponse

probe_router = APIRouter(tags=["infra"])
meta_router = APIRouter(tags=["infra"])


class HealthzResponse(BaseModel):
    status: str = "ok"
    name: str = APP_NAME
    version: str = __version__
    app_env: str


class ReadyzCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class ReadyzResponse(BaseModel):
    status: str
    checks: list[ReadyzCheck]


class MetaFeatures(BaseModel):
    """能力开关快照（6.3 第 5 条）。

    `mcp` / `memory` / `eval_platform` 在 MVP **恒为 false**（SD-15/16/18：能力未实现）；
    前端据 `features` 隐藏对应菜单，无需改契约（3.2.1）。
    """

    mcp: bool = False
    memory: bool = False
    eval_platform: bool = False
    python_execute: bool = False
    file_write: bool = False
    web_search: bool = False


class MetaData(BaseModel):
    name: str
    version: str
    app_env: str
    features: MetaFeatures


def build_features(settings: Settings) -> MetaFeatures:
    """生成 features 快照（启动时生成一次，存 `app.state.features`）。"""
    return MetaFeatures(
        mcp=False,  # SD-16：迭代 B 才实现
        memory=False,  # SD-15：迭代 A 才实现
        eval_platform=False,  # SD-18：MVP 用 scripts/evaluate.py
        python_execute=settings.python_execute_enabled,
        file_write=settings.file_write_enabled,
        web_search=bool(settings.web_search_provider and settings.web_search_api_key),
    )


def _check_vector_store(settings: Settings) -> ReadyzCheck:
    """向量库可达性（6.3 第 4 条）。

    Phase 0：`chroma` 校验目录存在且可写（启动时已创建，缺失说明卷没挂上）；
    Phase 5 接入 Chroma 后替换为真实的 collection 探测。
    """
    if settings.vector_store_kind == "memory":
        return ReadyzCheck(name="vector_store", ok=True, detail="memory (in-process)")
    path = settings.chroma_path
    if not path.exists():
        return ReadyzCheck(name="vector_store", ok=False, detail=f"directory missing: {path}")
    if not os.access(path, os.W_OK):
        return ReadyzCheck(name="vector_store", ok=False, detail=f"directory not writable: {path}")
    return ReadyzCheck(name="vector_store", ok=True, detail=f"writable: {path}")


@probe_router.get("/healthz", response_model=HealthzResponse, summary="存活探针")
async def healthz() -> HealthzResponse:
    """不查任何依赖（6.3）：只要进程还能响应就是 ok。"""
    return HealthzResponse(app_env=get_settings().app_env)


@probe_router.get(
    "/readyz",
    response_model=ReadyzResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyzResponse, "description": "依赖不可用"}},
    summary="就绪探针",
)
async def readyz(response: Response) -> ReadyzResponse:
    settings = get_settings()
    db_ok, db_detail = await db_session.check_database()
    checks = [
        ReadyzCheck(name="database", ok=db_ok, detail=db_detail),
        _check_vector_store(settings),
    ]
    healthy = all(check.ok for check in checks)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyzResponse(status="ok" if healthy else "degraded", checks=checks)


@meta_router.get("/meta", response_model=ApiResponse[MetaData], summary="版本与能力开关")
async def meta(request: Request) -> ApiResponse[MetaData]:
    """`features` 取启动时的快照（6.3 第 5 条）；未跑 lifespan 时（如单测直接调用）回退为即时计算。"""
    settings = get_settings()
    features = getattr(request.app.state, "features", None) or build_features(settings)
    return ApiResponse[MetaData].of(
        MetaData(
            name=APP_NAME,
            version=__version__,
            app_env=settings.app_env,
            features=features,
        )
    )
