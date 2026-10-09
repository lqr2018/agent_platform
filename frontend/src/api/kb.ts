/**
 * 知识库与文档（详细设计 3.2.5 / 5.1 / 7.6）。
 *
 * 端点分四组（与后端 `app/api/v1/knowledge_bases.py` 一一对应）：
 *
 * - **KB**：列表（`?q=`）/ 新建（后端自动探测 `embedding_dim`）/ 详情 / 更新 / 删除（软删 + 删向量集合）；
 * - **文档**：列表（`?status=`）/ 上传（multipart → 202）/ 详情 / 删除 / 重新摄取（202）；
 * - **切片**：`GET /{doc_id}/chunks`（分页，上限由后端 `MAX_CHUNK_PAGE_SIZE` 兜底）；
 * - **检索与运维**：`POST /query`（`kb_ids` 支持多库试算）、`GET /maintenance/.../verify-index` 对账。
 *
 * 上传后 `documents.status` 是**唯一进度判据**（4.6.2）：页面按 `DOCUMENT_POLL_INTERVAL_MS` 轮询，
 * 到 `ready` / `failed` 即停（`isDocumentSettled`）。
 */

import type { components } from "@/types/api";

import { apiFetch } from "./client";

export type KnowledgeBaseRead = components["schemas"]["KnowledgeBaseRead"];
export type KnowledgeBaseCreate = components["schemas"]["KnowledgeBaseCreate"];
export type KnowledgeBaseUpdate = components["schemas"]["KnowledgeBaseUpdate"];
export type DocumentRead = components["schemas"]["DocumentRead"];
export type ChunkRead = components["schemas"]["ChunkRead"];
export type QueryRequest = components["schemas"]["QueryRequest"];
export type QueryResultRead = components["schemas"]["QueryResultRead"];
export type QueryHitRead = components["schemas"]["QueryHitRead"];
export type MaintenanceResultRead = components["schemas"]["MaintenanceResultRead"];

/** 4.6.2 的进度轮询间隔（与 Workflow 运行的 2s 一致）。 */
export const DOCUMENT_POLL_INTERVAL_MS = 2000;

/** 切片预览分页上限（后端 `kb_service.MAX_CHUNK_PAGE_SIZE` 的同名口径）。 */
export const MAX_CHUNK_PAGE_SIZE = 200;

/** `pending/parsing/chunking/embedding` 都还在跑；`ready/failed` 是终态。 */
export function isDocumentSettled(status: string | undefined): boolean {
  return status === "ready" || status === "failed";
}

/** 3.2.5 本阶段只接受 md / txt（pdf 与 url 属迭代 E，后端会以 `KB_UNSUPPORTED_FORMAT` 422）。 */
export const UPLOAD_ACCEPT = ".md,.txt";

// ---- KB CRUD ----

export function listKnowledgeBases(params: { q?: string } = {}): Promise<KnowledgeBaseRead[]> {
  const query = new URLSearchParams();
  if (params.q) {
    query.set("q", params.q);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<KnowledgeBaseRead[]>(`/knowledge-bases${suffix}`);
}

export function getKnowledgeBase(id: string): Promise<KnowledgeBaseRead> {
  return apiFetch<KnowledgeBaseRead>(`/knowledge-bases/${id}`);
}

export function createKnowledgeBase(payload: KnowledgeBaseCreate): Promise<KnowledgeBaseRead> {
  return apiFetch<KnowledgeBaseRead>("/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 只传要改的字段（`embedding_model` 有文档后会被后端 422 拒绝，2.8）。 */
export function updateKnowledgeBase(id: string, payload: KnowledgeBaseUpdate): Promise<KnowledgeBaseRead> {
  return apiFetch<KnowledgeBaseRead>(`/knowledge-bases/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteKnowledgeBase(id: string): Promise<void> {
  return apiFetch<void>(`/knowledge-bases/${id}`, { method: "DELETE" });
}

// ---- 文档 ----

export function listDocuments(kbId: string, params: { status?: string } = {}): Promise<DocumentRead[]> {
  const query = new URLSearchParams();
  if (params.status) {
    query.set("status", params.status);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<DocumentRead[]>(`/knowledge-bases/${kbId}/documents${suffix}`);
}

/**
 * 上传文档（multipart → 202 + `pending`）。
 *
 * 不手写 `Content-Type`：让浏览器带上 `boundary`（手写会丢 boundary，后端解析 400）。
 */
export function uploadDocument(kbId: string, file: File): Promise<DocumentRead> {
  const form = new FormData();
  form.append("file", file, file.name);
  return apiFetch<DocumentRead>(`/knowledge-bases/${kbId}/documents`, { method: "POST", body: form });
}

export function getDocument(kbId: string, documentId: string): Promise<DocumentRead> {
  return apiFetch<DocumentRead>(`/knowledge-bases/${kbId}/documents/${documentId}`);
}

export function deleteDocument(kbId: string, documentId: string): Promise<void> {
  return apiFetch<void>(`/knowledge-bases/${kbId}/documents/${documentId}`, { method: "DELETE" });
}

/** 重新摄取（改了切片参数 / 上次失败后用）→ 202 + `pending`。 */
export function reingestDocument(kbId: string, documentId: string): Promise<DocumentRead> {
  return apiFetch<DocumentRead>(`/knowledge-bases/${kbId}/documents/${documentId}/reingest`, {
    method: "POST",
  });
}

/** 切片预览（分页）。 */
export function listDocumentChunks(
  kbId: string,
  documentId: string,
  params: { offset?: number; limit?: number } = {},
): Promise<ChunkRead[]> {
  const query = new URLSearchParams();
  if (params.offset !== undefined) {
    query.set("offset", String(params.offset));
  }
  if (params.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiFetch<ChunkRead[]>(`/knowledge-bases/${kbId}/documents/${documentId}/chunks${suffix}`);
}

// ---- 检索与运维 ----

/**
 * 直接检索（3.2.5：前端调试与评测用）。
 *
 * 传 `kb_ids` 时按多库合并重排（2.8），否则只用路径里的 KB；未命中是**正常结果**
 * （`hit_count=0` + `chunks=[]`），不抛错。
 */
export function queryKnowledgeBase(kbId: string, payload: QueryRequest): Promise<QueryResultRead> {
  return apiFetch<QueryResultRead>(`/knowledge-bases/${kbId}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 向量与 DB 的一致性对账（7.6；只读且幂等）。 */
export function verifyKnowledgeBaseIndex(kbId: string): Promise<MaintenanceResultRead> {
  return apiFetch<MaintenanceResultRead>(`/maintenance/knowledge-bases/${kbId}/verify-index`);
}
