# Phase 5（RAG 知识库）时序与链路

> 本文档按《详细设计》附录 F 规则 3，为 **Phase 5** 补的时序/流程图（Phase 0–3 见 `phase-0-bootstrap.md` /
> `phase-1-agent-runtime.md` / `phase-2-tool-calling.md` / `phase-3-workflow.md`）。
> 图中的事件名、表名、错误码与《详细设计》2.8 / 3.2.5 / 3.4 / 4.4.2 / 4.6 / 7.6 一致。

## 1. 摄取链路（上传 → `ready`）

```text
浏览器                 api/v1/knowledge_bases.py   services/kb_service.py + task_runner.py        db / 向量库
  │ POST /knowledge-bases/{id}/documents (multipart)
  ├──────────────────────────────────>│ ① 后缀白名单（.md/.txt；其余 → 422 KB_UNSUPPORTED_FORMAT）
  │                                   │ ② 落 data/uploads/ + sha256 checksum
  │                                   │ ③ documents 一行（status=pending）
  │<── 202 {id, status:"pending"} ────┤ ④ TaskRunner.submit(ingest_document)
  │ GET /documents/{doc_id}（2s 轮询）│
  │                                   │  三阶段（4.6.2 的状态机）
  │                                   │    parsing   → LoaderRegistry.load（md 的标题进 meta.heading）
  │                                   │    chunking  → build_splitter().split（chunk_size / overlap / 标题路径）
  │                                   │    embedding → Embedder.embed（批量）→ VectorStore.upsert 到集合 kb_{kb_id}
  │                                   │    ↑ 每阶段**先提交** documents.status（唯一进度判据）
  │ GET /documents/{doc_id} ─────────>│ status ∈ {pending, parsing, chunking, embedding, ready, failed}
  │ GET /documents/{doc_id}/chunks ──>│ chunks 行（`id` = `vector_id`，2.8）
```

要点：

- **幂等（4.6.2）**：同一 KB 内 checksum 已存在 → 复用已有切片（`meta.reused_from_document_id`），
  第二次上传**不再调用 embedding**（`test_second_upload_with_same_checksum_skips_embedding` 钉住调用次数）；
- **切片 id = 向量 id**：删除文档时「先删向量、再删行」，因此 `verify-index` 两端计数恒相等（7.6 的 DoD）；
- **进程退出**：`TaskRunner.stop()` 排空/取消在途任务，启动时 `converge_interrupted_documents()` 把
  `parsing/chunking/embedding` 收敛为 `failed`（可 `reingest`）—— 4.6.2 只写了"可 reingest"，收敛点见附录 F v1.16。

## 2. 检索链路（两条入口，同一个 `Retriever`）

```text
① Chat 的固定预检索（4.4.2 模式 a）
POST /conversations/{id}/messages
  └─ chat_service.start_chat
       ├─ kb_service.retrieve(session, agents.knowledge_base_ids, user_input)   # 首轮 LLM 之前
       │    ├─ 多 KB：要求同一 embedding_model（否则 422 VALIDATION_ERROR；不同空间分数不可比）
       │    ├─ Retriever：逐 KB 检索 → 合并 → 余弦排序（rerank 插点预留给迭代 E）
       │    └─ span `retriever:{kb_ids}`：attributes = {kb_ids, strategy=prefetch, hit_count, used_kb_ids}
       ├─ emit(retrieval.completed)      # 事件 10：{kb_ids, query, hit_count}
       ├─ 命中 → build_context_blocks()：`<chunk id source>…</chunk>` 追加进 system（4.6.3）
       │  未命中 → EMPTY_RETRIEVAL_NOTICE（**不注入空段落**）
       │  检索异常 → 记日志 + hit_count=0 + 同一句提示（**不打断对话**）
       └─ runtime.run(..., retrieved_context=…, retrieval_notice=…)

② Workflow 的 `retriever` 节点（4.5.2）
POST /workflows/{id}/runs → SimpleEngine → 节点 retrieve
  └─ ServiceNodeRunner.run_retriever_node
       ├─ query = render(query_template)（默认 {{state.input}}）
       ├─ kb_service.retrieve([kb_id], query)        # kb_id 必须是**已存在的知识库 id**
       ├─ state[output_key] = [{chunk_id, document_id, kb_id, score, content, source}]
       │   └─ 未命中 / 空索引 → []（**不算节点失败**：由 condition 分支决定怎么答）
       ├─ emit(retrieval.completed)                  # Chat 内联场景前端可见；手动运行是 NullEmitter
       └─ KB 不存在 → KB_NOT_FOUND → 引擎记 node_runs.error_code 后按 on_error（默认 continue）
```

## 3. 引用格式（4.6.3：三处口径必须一致）

```text
system 注入的块      <chunk id="01J…" source="install.md#2.1 page=2 score=0.83">…</chunk>
POST /query 的返回   {chunk_id, document_id, kb_id, score, source, content}
workflow 节点产物    {chunk_id, document_id, kb_id, score, content, source}   # 供下游 agent 节点引用
```

`source` 由 `RetrievedChunk.citation_source()` **单点生成**：`{filename|document_id}[#heading][ page=N] score=xx.xx`
—— Chat 注入、`/query` 响应、`retriever` 节点产物共用它，避免三处各写一套拼接。

## 4. 一致性与运维（7.6 / 6.3）

```text
GET /maintenance/knowledge-bases/{id}/verify-index
   → {ok, data:{db_chunks, vector_items}}                 # SQLite ↔ collection 两端计数
GET /readyz → checks.vector_store
   → chroma：PersistentClient.heartbeat()（按目录缓存 client）
   → memory：恒 ok（进程内）
VECTOR_STORE_KIND=memory：InMemoryVectorStore 是**进程级单例**，lifespan 启动时重置
   → 否则"摄取写入的向量"与"检索读的向量"落在两个库上（VECTOR_STORE_KIND=memory 表现为永远检索不到）
```

## 5. 端点与前端落点（3.2.5 / 5.1）

```text
知识库页面 pages/KnowledgePage.tsx   api/kb.ts            app/api/v1/knowledge_bases.py
  列表 / 新建 / 编辑 / 删除    ──────> listKnowledgeBases   GET/POST/PATCH/DELETE /knowledge-bases[/{id}]
  上传（multipart）             ──────> uploadDocument      POST   /knowledge-bases/{id}/documents
  进度轮询（2s）                ──────> listDocuments       GET    /knowledge-bases/{id}/documents
  切片预览                      ──────> listDocumentChunks  GET    /knowledge-bases/{id}/documents/{doc_id}/chunks
  检索试算（可多 KB）           ──────> queryKnowledgeBase  POST   /knowledge-bases/{id}/query
Chat 引用来源卡片 components/chat/CitationCard.tsx ← chatStore.citations ← SSE 事件 10
```

- 前端已落地（附录 F v1.18）：`api/kb.ts` / `KnowledgePage`（文档表 2s 轮询、切片预览弹窗、检索试算、索引对账）
  / `CitationCard`（Chat 侧消费事件 10，用**同一 query** 调 `/query` 补拉明细）；`AgentEditPage` 的
  `knowledge_base_ids` 多选就是预检索的开关（未绑定 → 不发事件 10）；
- `kb_search`（未绑定 KB 时由模型按需检索）属**迭代 E**，与 `PdfLoader` / `WebLoader` / `Reranker` 一起做（4.4.2 / 4.6.1）；
- 引用卡片与「工具调用卡片」并列渲染：都是"过程"，与消息"内容"分开（与 Phase 2 的展示口径一致）。
