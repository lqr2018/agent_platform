/**
 * HTTP 客户端与统一响应封装（详细设计 3.1 / 1.6）。
 *
 * - `/api/v1` 下的接口返回 `{ data, meta }`，成功时本模块**解包 data**；
 * - 失败时后端返回 `{ error, meta }`，抛 `ApiError`（含 `code` / `status` / `requestId`）；
 * - 根路径探针（`/healthz`、`/readyz`）不走信封，用 `fetchProbe`。
 */

import type { components } from "@/types/api";

export type ResponseMeta = components["schemas"]["ResponseMeta"];

/**
 * 错误体（1.6）。手写而非取自 `types/api.d.ts`：错误响应通过异常处理器返回，
 * 不参与 OpenAPI 的 `response_model`，因此不在生成结果里。
 */
export interface ErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface ApiEnvelope<T> {
  data: T;
  meta: ResponseMeta;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;
  readonly requestId: string;

  constructor(params: {
    message: string;
    code: string;
    status: number;
    details?: Record<string, unknown>;
    requestId?: string;
  }) {
    super(params.message);
    this.name = "ApiError";
    this.code = params.code;
    this.status = params.status;
    this.details = params.details ?? {};
    this.requestId = params.requestId ?? "";
  }
}

const API_BASE = "/api/v1";

export { API_BASE };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseJson(text: string): unknown {
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function toApiError(status: number, payload: unknown): ApiError {
  if (isRecord(payload) && isRecord(payload["error"])) {
    const error = payload["error"];
    const meta = isRecord(payload["meta"]) ? payload["meta"] : {};
    const details = isRecord(error["details"]) ? error["details"] : {};
    return new ApiError({
      message: typeof error["message"] === "string" ? error["message"] : `HTTP ${status}`,
      code: typeof error["code"] === "string" ? error["code"] : "INTERNAL_ERROR",
      status,
      details,
      requestId: typeof meta["request_id"] === "string" ? meta["request_id"] : "",
    });
  }
  return new ApiError({ message: `HTTP ${status}`, code: "INTERNAL_ERROR", status });
}

async function readPayload(response: Response): Promise<unknown> {
  return parseJson(await response.text());
}

/** `/api/v1` 下的请求：成功返回解包后的 `data`。 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const envelope = await apiFetchEnvelope<T>(path, init);
  return envelope.data;
}

/** 需要读 `meta`（分页 / 游标）时用这个：返回完整信封。 */
export async function apiFetchEnvelope<T>(path: string, init?: RequestInit): Promise<ApiEnvelope<T>> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (response.status === 204) {
    return { data: undefined as T, meta: { request_id: response.headers.get("X-Request-Id") ?? "" } };
  }
  const payload = await readPayload(response);
  if (!response.ok) {
    throw toApiError(response.status, payload);
  }
  if (!isRecord(payload) || !("data" in payload)) {
    throw new ApiError({
      message: "Malformed response: missing `data` envelope",
      code: "INTERNAL_ERROR",
      status: response.status,
    });
  }
  const meta = isRecord(payload["meta"]) ? payload["meta"] : {};
  return { data: payload["data"] as T, meta: meta as ResponseMeta };
}

/** 根路径探针（`/healthz`、`/readyz`）：响应体是裸对象。 */
export async function fetchProbe<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await readPayload(response);
  if (!response.ok) {
    throw toApiError(response.status, payload);
  }
  return payload as T;
}
