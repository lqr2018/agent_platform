/**
 * `api/kb.ts` 的请求形状（详细设计 3.2.5）。
 *
 * 只钉"URL / 方法 / body"这三件前端真正决定的事；响应解包由 `client.test.ts` 覆盖。
 * 上传这条特别钉住：**不能手写 `Content-Type`**（会丢 multipart 的 `boundary`）。
 */

import { describe, expect, it, vi } from "vitest";

import {
  DOCUMENT_POLL_INTERVAL_MS,
  createKnowledgeBase,
  deleteDocument,
  isDocumentSettled,
  listDocumentChunks,
  listDocuments,
  listKnowledgeBases,
  queryKnowledgeBase,
  reingestDocument,
  uploadDocument,
  verifyKnowledgeBaseIndex,
} from "@/api/kb";

function stubFetch(payload: unknown, status = 200): void {
  // 204 不能带 body（`new Response(body, {status: 204})` 在 undici 下会抛错）。
  const body = status === 204 ? null : JSON.stringify(payload);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(body, {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

function envelope(data: unknown) {
  return { data, meta: { request_id: "req-1" } };
}

function lastCall(): [string, RequestInit | undefined] {
  const calls = vi.mocked(fetch).mock.calls;
  const call = calls[calls.length - 1];
  if (!call) {
    throw new Error("fetch was not called");
  }
  return [String(call[0]), call[1]];
}

describe("知识库 API", () => {
  it("列表支持 ?q=", async () => {
    stubFetch(envelope([]));

    await listKnowledgeBases({ q: "install" });

    expect(lastCall()[0]).toBe("/api/v1/knowledge-bases?q=install");
  });

  it("新建 KB 用 POST + JSON body（embedding_dim 由后端探测，前端不传）", async () => {
    stubFetch(envelope({ id: "kb_1" }), 201);

    await createKnowledgeBase({
      name: "handbook",
      description: "",
      embedding_provider_id: "01J0T0000000000000000000A0",
      embedding_model: "text-embedding-3-small",
      splitter: "markdown",
      chunk_size: 800,
      chunk_overlap: 120,
      top_k: 5,
      score_threshold: 0.3,
    });

    const [url, init] = lastCall();
    expect(url).toBe("/api/v1/knowledge-bases");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      name: "handbook",
      splitter: "markdown",
      top_k: 5,
    });
    expect(JSON.parse(String(init?.body))).not.toHaveProperty("embedding_dim");
  });

  it("上传走 multipart，且不手写 Content-Type（boundary 必须由浏览器生成）", async () => {
    stubFetch(envelope({ id: "doc_1", status: "pending" }), 202);
    const file = new File(["# 安装"], "install.md", { type: "text/markdown" });

    await uploadDocument("kb_1", file);

    const [url, init] = lastCall();
    expect(url).toBe("/api/v1/knowledge-bases/kb_1/documents");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeInstanceOf(FormData);
    const form = init?.body as FormData;
    const attached = form.get("file") as File;
    expect(attached.name).toBe("install.md");
    expect(attached.type).toBe("text/markdown");
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
  });

  it("文档列表支持 ?status= 过滤", async () => {
    stubFetch(envelope([]));

    await listDocuments("kb_1", { status: "failed" });

    expect(lastCall()[0]).toBe("/api/v1/knowledge-bases/kb_1/documents?status=failed");
  });

  it("切片预览带 offset / limit", async () => {
    stubFetch(envelope([]));

    await listDocumentChunks("kb_1", "doc_1", { offset: 20, limit: 20 });

    expect(lastCall()[0]).toBe("/api/v1/knowledge-bases/kb_1/documents/doc_1/chunks?offset=20&limit=20");
  });

  it("重新摄取 / 删除文档", async () => {
    stubFetch(envelope({ id: "doc_1", status: "pending" }), 202);
    await reingestDocument("kb_1", "doc_1");
    expect(lastCall()).toEqual([
      "/api/v1/knowledge-bases/kb_1/documents/doc_1/reingest",
      expect.objectContaining({ method: "POST" }),
    ]);

    stubFetch(envelope(undefined), 204);
    await deleteDocument("kb_1", "doc_1");
    expect(lastCall()).toEqual([
      "/api/v1/knowledge-bases/kb_1/documents/doc_1",
      expect.objectContaining({ method: "DELETE" }),
    ]);
  });

  it("检索试算：路径带主 KB，body 带 kb_ids 支持多库合并；未命中是正常结果", async () => {
    stubFetch(envelope({ query: "怎么安装", kb_ids: ["kb_1", "kb_2"], hit_count: 0, chunks: [] }));

    const result = await queryKnowledgeBase("kb_1", { query: "怎么安装", kb_ids: ["kb_1", "kb_2"] });

    const [url, init] = lastCall();
    expect(url).toBe("/api/v1/knowledge-bases/kb_1/query");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ query: "怎么安装", kb_ids: ["kb_1", "kb_2"] });
    expect(result.hit_count).toBe(0);
    expect(result.chunks).toEqual([]);
  });

  it("verify-index 走 /maintenance（运维只读端点）", async () => {
    stubFetch(envelope({ ok: true, detail: "chunks and vector items match", data: {} }));

    const result = await verifyKnowledgeBaseIndex("kb_1");

    expect(lastCall()[0]).toBe("/api/v1/maintenance/knowledge-bases/kb_1/verify-index");
    expect(result.ok).toBe(true);
  });
});

describe("文档进度口径（4.6.2）", () => {
  it("只有 ready / failed 是终态，其余状态都需要继续轮询", () => {
    expect(isDocumentSettled("ready")).toBe(true);
    expect(isDocumentSettled("failed")).toBe(true);
    for (const status of ["pending", "parsing", "chunking", "embedding", undefined]) {
      expect(isDocumentSettled(status)).toBe(false);
    }
  });

  it("轮询间隔与 Workflow 运行一致（2s）", () => {
    expect(DOCUMENT_POLL_INTERVAL_MS).toBe(2000);
  });
});
