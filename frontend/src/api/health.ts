/**
 * 基础设施探针与能力开关（详细设计 3.2.1 / 6.3）。
 *
 * `/healthz`、`/readyz` 在根路径且不是信封结构；`/api/v1/meta` 走 `/api/v1` 信封。
 */

import type { components } from "@/types/api";

import { apiFetch, fetchProbe } from "./client";

export type HealthzResponse = components["schemas"]["HealthzResponse"];
export type ReadyzCheck = components["schemas"]["ReadyzCheck"];
export type ReadyzResponse = components["schemas"]["ReadyzResponse"];
export type MetaFeatures = components["schemas"]["MetaFeatures"];
export type PlatformMeta = components["schemas"]["MetaData"];

/** 进程存活（不查依赖）。 */
export function getHealthz(): Promise<HealthzResponse> {
  return fetchProbe<HealthzResponse>("/healthz");
}

/** 就绪探针：DB 可写 + 迁移到 head + 向量库可达；依赖不可用时后端返回 503。 */
export function getReadyz(): Promise<ReadyzResponse> {
  return fetchProbe<ReadyzResponse>("/readyz");
}

/** 版本与能力开关快照（前端据此隐藏 Backlog 菜单，5.2 / SD-14②）。 */
export function getPlatformMeta(): Promise<PlatformMeta> {
  return apiFetch<PlatformMeta>("/meta");
}
