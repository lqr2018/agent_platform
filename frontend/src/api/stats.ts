/**
 * 平台指标（详细设计 3.2.9 / 4.8.3 / 5.2 的 `/` 看板）。
 *
 * 字段名与 `data/reports/eval-*.json` 的 `metrics` 段**共用一套**（`success_rate` /
 * `avg_latency_ms` / `avg_tool_calls` / `avg_total_tokens` / `total_cost_usd` / `error_rate` /
 * `tool_success_rate`），所以看板与评测报告展示的是同一组名字。
 *
 * 比率与均值在"窗口内没有已收敛 Run"时后端返回 `null` 而不是 `0.0`（4.8.3 的口径：
 * 未收敛的行不进分母），前端必须把 `null` 显示成 `-`，否则会被读成"成功率 0%"。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type StatsOverview = components["schemas"]["StatsOverview"];

export interface StatsOverviewQuery {
  /** 窗口起点（闭区间，ISO 8601 UTC）；不传表示不限。 */
  from?: string;
  to?: string;
  /** `chat` / `workflow` / `eval`；不传统计全部来源（7.8 补的可选收窄项）。 */
  kind?: string;
}

/** 窗口内的 Run 数 / 成功率 / 平均时延 / 平均工具调用 / token / 成本（`GET /stats/overview`）。 */
export function getStatsOverview(params: StatsOverviewQuery = {}): Promise<StatsOverview> {
  const query = new URLSearchParams();
  if (params.from) {
    query.set("from", params.from);
  }
  if (params.to) {
    query.set("to", params.to);
  }
  if (params.kind) {
    query.set("kind", params.kind);
  }
  const suffix = query.toString();
  return apiFetch<StatsOverview>(`/stats/overview${suffix ? `?${suffix}` : ""}`);
}
