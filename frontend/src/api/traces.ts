/**
 * Trace 只读接口（详细设计 3.2.9 / 5.1）。
 *
 * 列表用游标分页（`meta.next_cursor`）；Trace 详情返回**扁平 span 列表**，由前端建树建树（`components/trace/TraceTree.tsx`）。
 */

import type { components } from "@/types/api";

import { type ApiEnvelope, apiFetch, apiFetchEnvelope } from "./client";

export type TraceRead = components["schemas"]["TraceRead"];
export type TraceDetail = components["schemas"]["TraceDetail"];
export type SpanRead = components["schemas"]["SpanRead"];
export type SpanSummary = components["schemas"]["SpanSummary"];
export type RunRead = components["schemas"]["RunRead"];

const TRACE_PAGE_SIZE = 50;

/** Trace 列表（游标分页，`?kind=&status=&limit=&cursor=`）。 */
export function listTraces(
  params: { kind?: string; status?: string; limit?: number; cursor?: string } = {},
): Promise<ApiEnvelope<TraceRead[]>> {
  const query = new URLSearchParams();
  if (params.kind) {
    query.set("kind", params.kind);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  query.set("limit", String(params.limit ?? TRACE_PAGE_SIZE));
  if (params.cursor) {
    query.set("cursor", params.cursor);
  }
  return apiFetchEnvelope<TraceRead[]>(`/traces?${query.toString()}`);
}

/** Trace 概要 + 扁平 span 列表（前端自行建树，3.2.9）。 */
export function getTrace(traceId: string): Promise<TraceDetail> {
  return apiFetch<TraceDetail>(`/traces/${traceId}`);
}

/** 单个 span（含完整 `input` / `output`）。 */
export function getSpan(spanId: string): Promise<SpanRead> {
  return apiFetch<SpanRead>(`/spans/${spanId}`);
}
