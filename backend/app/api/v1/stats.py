"""指标路由（详细设计 3.2.9 / 4.8.3 / 7.8 任务 5）。

`GET /stats/overview`：窗口内（`?from=&to=`，可再按 `?kind=` 收窄）的基础指标 ——
Run 数 / 成功率 / 平均时延 / 平均工具调用 / token / 成本 / 错误率，全部来自 `runs` / `spans`
的 SQL 聚合（4.8.3：SQLite 用 SQL 聚合即可，不引入时序库）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ApiResponse
from app.schemas.stats import StatsOverview
from app.services import stats_service

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get(
    "/overview",
    response_model=ApiResponse[StatsOverview],
    summary="基础指标（成功率 / 时延 / 工具调用 / token / 成本）",
)
async def get_overview(
    session: SessionDep,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    kind: str | None = None,
) -> ApiResponse[StatsOverview]:
    """3.2.9 的基础指标；`from` / `to` 与 `GET /traces` 同口径（闭区间，naive UTC 落库）。

    `kind`（`chat` / `workflow` / `eval`）是本次实现补的可选收窄项 —— 评测用例也落 `runs`，
    不过滤时"成功率"会把评测批次算进来（见 7.8）；不外传时统计全部来源。
    """
    totals = await stats_service.overview(session, from_at=from_at, to_at=to_at, kind=kind)
    return ApiResponse[StatsOverview].of(StatsOverview.model_validate(totals))
