import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DashboardPage from "@/pages/DashboardPage";

/**
 * 总览页的接线（5.2 的 `/`）：`/stats` 看板的六项指标 + 空窗口口径。
 *
 * 这里只验证页面能把 `GET /stats/overview` 的结果渲染成数字，以及**`null` 不显示成 0**
 * （4.8.3：没有分母时后端给 `null`）；阈值与公式本身由 `tests/integration/test_stats_api.py` 钉住。
 */

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DashboardPage />
    </QueryClientProvider>,
  );
}

const HEALTHZ = { name: "agent-platform", version: "0.1.0", app_env: "test", status: "ok" };
const READYZ = { status: "ok", checks: [{ name: "database", ok: true, detail: "revision 0005" }] };
const META = { features: { memory: false, mcp: false, evaluation: false } };

function envelope(data: unknown): Response {
  return new Response(JSON.stringify({ data, meta: { request_id: "req-1" } }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function probe(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** 按路径分派：`/stats/overview` 与 `/meta` 走信封，`/healthz` / `/readyz` 是裸对象。 */
function stubFetch(stats: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("stats/overview")) {
        return envelope(stats);
      }
      if (url.includes("/meta")) {
        return envelope(META);
      }
      if (url.includes("/readyz")) {
        return probe(READYZ);
      }
      return probe(HEALTHZ);
    }),
  );
}

describe("DashboardPage", () => {
  it("渲染六项指标与合计（成功率 / 时延 / 工具调用 / token / 成本）", async () => {
    stubFetch({
      generated_at: "2026-10-13T00:00:00Z",
      total_runs: 4,
      succeeded: 3,
      failed: 1,
      canceled: 0,
      running: 0,
      terminal_runs: 4,
      success_rate: 0.75,
      error_rate: 0.25,
      avg_latency_ms: 1234.5,
      avg_tool_calls: 2,
      avg_total_tokens: 320.25,
      total_tokens: 1281,
      total_cost_usd: 0.0123,
      tool_spans_total: 2,
      tool_spans_succeeded: 2,
      tool_success_rate: 1,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText("75.0%")).toBeInTheDocument());
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText("1234.5 ms")).toBeInTheDocument();
    // antd 会把纯数字拆成 int / decimal 两个节点（`320` + `.3`），所以按卡片容器断言文本
    expect(screen.getByText("平均 token").closest(".ant-statistic")).toHaveTextContent("320.3");
    expect(screen.getByText("$0.0123")).toBeInTheDocument();
    expect(screen.getByText("100.0%（2/2 次 tool span）")).toBeInTheDocument();
    expect(screen.getByText(/截至 2026-10-13T00:00:00Z/)).toBeInTheDocument();
  });

  it("窗口内没有已收敛 Run 时比率显示 `-` 并给出提示（不显示成 0%）", async () => {
    stubFetch({
      generated_at: "2026-10-13T00:00:00Z",
      total_runs: 0,
      succeeded: 0,
      failed: 0,
      canceled: 0,
      running: 0,
      terminal_runs: 0,
      success_rate: null,
      error_rate: null,
      avg_latency_ms: null,
      avg_tool_calls: null,
      avg_total_tokens: null,
      total_tokens: 0,
      total_cost_usd: 0,
      tool_spans_total: 0,
      tool_spans_succeeded: 0,
      tool_success_rate: null,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/窗口内还没有已收敛的 Run/)).toBeInTheDocument());
    expect(screen.queryByText("0.0%")).not.toBeInTheDocument();
    expect(screen.getAllByText("-").length).toBeGreaterThanOrEqual(3);
  });
});
