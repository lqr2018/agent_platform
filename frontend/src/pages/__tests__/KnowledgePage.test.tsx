import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import KnowledgePage from "@/pages/KnowledgePage";

/**
 * 知识库页的接线（5.2 的 `/knowledge`）：列表渲染 + 空态。
 *
 * 12 个端点的请求形状由 `api/__tests__/kb.test.ts` 钉住；这里只验证页面能把
 * `GET /knowledge-bases` 的结果渲染出来（含 `stats` 与 embedding 口径）。
 */

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <KnowledgePage />
    </QueryClientProvider>,
  );
}

function stubFetch(data: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data, meta: { request_id: "req-1" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

const KB_ROW = {
  id: "01J0T000000000000000000KB1",
  name: "handbook",
  description: "员工手册",
  embedding_provider_id: "01J0T0000000000000000000P0",
  embedding_model: "text-embedding-3-small",
  embedding_dim: 1536,
  vector_store_kind: "chroma",
  collection_name: "kb_01J0T000000000000000000KB1",
  splitter: "markdown",
  chunk_size: 800,
  chunk_overlap: 120,
  retriever_kind: "vector",
  top_k: 5,
  score_threshold: 0.3,
  rerank_enabled: false,
  stats: { document_count: 2, chunk_count: 7 },
  status: "ready",
  created_at: "2026-10-09T00:00:00Z",
  updated_at: "2026-10-09T00:00:00Z",
};

describe("KnowledgePage", () => {
  it("列出知识库并展示 embedding 口径与文档 / 切片统计", async () => {
    stubFetch([KB_ROW]);

    renderPage();

    await waitFor(() => expect(screen.getByText("handbook")).toBeInTheDocument());
    expect(screen.getByText(/text-embedding-3-small（dim=1536 · chroma）/)).toBeInTheDocument();
    expect(screen.getByText("2 / 7")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  it("没有知识库时给出空态提示（不建假数据）", async () => {
    stubFetch([]);

    renderPage();

    await waitFor(() => expect(screen.getByText(/还没有知识库/)).toBeInTheDocument());
  });
});
