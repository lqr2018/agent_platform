import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import ReadinessBadge from "@/components/common/ReadinessBadge";

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function stubReadyz(payload: unknown, status: number): void {
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

describe("ReadinessBadge", () => {
  it("就绪时显示『就绪』", async () => {
    stubReadyz(
      {
        status: "ok",
        checks: [
          { name: "database", ok: true, detail: "revision 0001_phase0_baseline" },
          { name: "vector_store", ok: true, detail: "writable: data/chroma" },
        ],
      },
      200,
    );

    renderWithQueryClient(<ReadinessBadge />);

    await waitFor(() => expect(screen.getByText(/就绪/)).toBeInTheDocument());
  });

  it("后端 503 时显示『后端不可达』", async () => {
    stubReadyz(
      {
        error: { code: "NOT_IMPLEMENTED", message: "Service Unavailable", details: {} },
        meta: { request_id: "req-503" },
      },
      503,
    );

    renderWithQueryClient(<ReadinessBadge />);

    await waitFor(() => expect(screen.getByText(/后端不可达/)).toBeInTheDocument());
  });
});
