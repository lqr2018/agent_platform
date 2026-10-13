"""指标看板 DTO（详细设计 3.2.9 / 4.8.3 / 7.8 任务 5）。

字段名与 4.9.2 的报告口径**共用一套**（`success_rate` / `avg_latency_ms` / `avg_tool_calls` /
`avg_total_tokens` / `total_cost_usd` / `error_rate` / `tool_success_rate`），前端 `/stats`
看板与 `data/reports/eval-*.json` 的 `metrics` 段展示的是同一组名字。

口径（公式）写在 `services/stats_service.py` 的模块 docstring 里，本文件只负责形状。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UtcDatetime


class StatsOverview(BaseModel):
    """`GET /stats/overview`（3.2.9：Run 数 / 成功率 / 平均时延 / 平均工具调用 / token / 成本）。"""

    model_config = ConfigDict(from_attributes=True)

    generated_at: UtcDatetime
    """本次聚合的时刻（前端显示"截至 …"）。"""

    from_at: UtcDatetime | None = None
    """请求里的 `?from=` 回显（未传 → `null`）。"""

    to_at: UtcDatetime | None = None
    """请求里的 `?to=` 回显（未传 → `null`）。"""

    kind: str | None = None
    """请求里的 `?kind=` 回显（未传表示统计全部来源：`chat` / `workflow` / `eval`）。"""

    total_runs: int = 0
    succeeded: int = 0
    failed: int = 0
    canceled: int = 0
    running: int = 0
    """未收敛的 Run（`pending` / `running` 合并计数）。"""

    terminal_runs: int = 0
    """已收敛的 Run（`succeeded` + `failed` + `canceled`）；比率类指标的分母。"""

    success_rate: float | None = None
    """`succeeded / terminal_runs`；`terminal_runs = 0` → `null`（不报 0.0 以免误读）。"""

    error_rate: float | None = None
    """`failed / terminal_runs`；取消不算错误（4.9.2 的 `error` 口径）。"""

    avg_latency_ms: float | None = None
    avg_tool_calls: float | None = None
    avg_total_tokens: float | None = None
    """三个均值只统计**收敛**的 Run（未收敛的行这几个字段还是默认值）；无样本 → `null`。"""

    total_tokens: int = 0
    total_cost_usd: float = 0.0
    """窗口内全部收敛 Run 的用量/成本合计（成本为 4.8.3 的估算值）。"""

    tool_spans_total: int = 0
    tool_spans_succeeded: int = 0
    tool_success_rate: float | None = None
    """`status=ok` 的 tool span 占比（跨本窗口的 `traces` 归属）；无 tool span → `null`。"""
