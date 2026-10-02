import { describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, fetchProbe } from "@/api/client";

function stubFetch(payload: unknown, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

describe("apiFetch", () => {
  it("解包 { data, meta } 并返回 data", async () => {
    stubFetch({ data: { name: "agent-platform", version: "0.1.0" }, meta: { request_id: "req-1" } });

    const data = await apiFetch<{ name: string; version: string }>("/meta");

    expect(data).toEqual({ name: "agent-platform", version: "0.1.0" });
  });

  it("请求路径带 /api/v1 前缀", async () => {
    stubFetch({ data: {}, meta: { request_id: "req-1" } });

    await apiFetch("/meta");

    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/meta",
      expect.objectContaining({ headers: expect.anything() }),
    );
  });

  it("错误响应抛 ApiError，并带上 code / status / requestId / details", async () => {
    stubFetch(
      {
        error: { code: "NOT_FOUND", message: "Agent not found", details: { agent_id: "01H" } },
        meta: { request_id: "req-404" },
      },
      404,
    );

    await expect(apiFetch("/agents/01H")).rejects.toMatchObject({
      name: "ApiError",
      code: "NOT_FOUND",
      status: 404,
      message: "Agent not found",
      requestId: "req-404",
      details: { agent_id: "01H" },
    });
  });

  it("响应缺少 data 信封时抛 ApiError", async () => {
    stubFetch({ unexpected: true });

    const error = await apiFetch("/meta").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toContain("Malformed response");
  });

  it("非 JSON 响应体不会抛解析异常", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>oops</html>", { status: 500 })));

    const error = await apiFetch("/meta").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("INTERNAL_ERROR");
    expect((error as ApiError).status).toBe(500);
  });
});

describe("fetchProbe", () => {
  it("根路径探针返回裸对象", async () => {
    stubFetch({ status: "ok", name: "agent-platform", version: "0.1.0", app_env: "test" });

    const body = await fetchProbe<{ status: string }>("/healthz");

    expect(body.status).toBe("ok");
    expect(vi.mocked(fetch)).toHaveBeenCalledWith("/healthz", expect.anything());
  });
});
