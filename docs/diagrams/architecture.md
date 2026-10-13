# 总架构（分层、运行时组件、数据模型、一次对话）

> 本文件是**汇总图**：把分散在各 Phase 图里的关键事实画在一处，给 README 的「架构」一节引用。
> 细节仍以各 Phase 图与《详细设计》正文为准（本文不引入新口径）：
> `phase-0-bootstrap.md`、`phase-1-agent-runtime.md`、`phase-2-tool-calling.md`、
> `phase-3-workflow.md`、`phase-5-rag.md`、`phase-7-eval.md`、`phase-8-demo.md`。

## 1. 分层与依赖方向（1.2 的分层不可逆）

```text
                    ┌──────────────────────────────┐
   浏览器  ────────▶│ frontend/ (Vite + React)      │
                    │  pages/* → api/* → client.ts  │
                    └───────────────┬──────────────┘
                       /api/v1/*（HTTP + SSE）与 /healthz、/readyz
                                    ▼
   ┌───────────────────────────────────────────────────────────┐
   │ api/          协议转换、参数校验、组装 DTO（不含业务规则）  │
   ├───────────────────────────────────────────────────────────┤
   │ services/     事务边界、实体 CRUD、跨实体编排（唯一写库者） │
   ├───────────────────────────────────────────────────────────┤
   │ runtime/      纯执行：agent / tools / workflow / rag / eval │
   │               （不 import services、不 import api）         │
   │ observability/ Tracer：Run / Span 的上下文传播              │
   ├───────────────────────────────────────────────────────────┤
   │ db/           models（ORM）+ session（async engine）        │
   └───────────────────────────────────────────────────────────┘
                    ▼                        ▼
              SQLite（app.db）        Chroma（向量库，data/chroma）
```

- **可替换家族**都是 `base.py`（Protocol / 抽象）+ `registry.py`（按 `kind` 实例化）：
  `LLMProvider`、`Embedder`、`VectorStore`、`WorkflowEngine`、`MemoryStore`、`Tool`。
- **单向依赖**：`api → services → (runtime, db/models)`；`runtime → db/base` 仅限类型与纯数据，
  执行逻辑通过入参 dataclass 拿数据（所以 `runtime/**` 能脱离 DB 单测）。
- **执行与观测的分界**：SSE 事件名由 `core/events.py` 单点定义（附录 B 的 21 个），
  文档 3.4 ↔ 代码由 `test_sse_contract.py` 钉住。

## 2. 运行时组件（一次 Run 里谁在干活）

| 组件 | 落点 | 职责 |
|---|---|---|
| `AgentRuntime` | `runtime/agent/` | 把"消息 + 工具 + 知识库"组装成一次循环；`max_steps` / 超时 / 取消的收敛点（4.4.3） |
| `Retriever` | `runtime/rag/` | 固定预检索（`knowledge_bases` 非空）与工具式检索共用同一个实现（4.6.3） |
| `ToolExecutor` | `runtime/tools/` | 9 步流水线：校验 → 权限分级 → 沙箱 → 执行 → 结果落库（4.2.3） |
| `SimpleEngine` | `runtime/workflow/` | 串行图执行、条件路由、checkpoint / resume；`NESTED_WORKFLOW_NOT_SUPPORTED` 守卫（4.5.4 / 4.4.4） |
| `Tracer` | `runtime/observability/` | Run → agent → llm / tool / retriever 的 span 树与 `trace_id` 传播（4.8） |
| `TaskRunner` | `services/task_runner.py` | 进程内队列（摄取 / 评测），单进程 asyncio + 限流（SD-9 / 4.6.2） |
| `EvalRunner` | `runtime/eval/` | 用例集 → 执行 → 7 类断言 → 指标 → 报告（4.9） |

## 3. 数据模型（MVP 生效的 16 张表）

```text
 配置侧                            运行侧                          知识侧
 model_providers ─┐              conversations ──── messages      knowledge_bases
 agents ──────────┼── runs ──────┴── traces ─────── spans          documents
   └ agent_prompt_versions        └ runs.kind = chat/workflow/eval    └ chunks
 tools ────────────┘  (runs 1—n tool_invocations)                        └ Chroma collection
 workflows ──── workflow_runs ──── node_runs
```

- **判据唯一**：一个 Run 的状态只看 `runs.status`；一个文档是否可检索只看 `documents.status`（4.6.2）。
- **Backlog（不在库里、不建空表，SD-14②）**：`memory_items`、`mcp_servers`、`eval_suites` /
  `eval_cases` / `eval_runs` / `eval_case_results` —— 评测结果 MVP 落 `data/reports/` 文件 +
  `runs.kind=eval`（2.12）。
- 迁移：`0001_phase0_baseline` → `0005_phase5_knowledge_tables`；Phase 7/8 **无新增表**
  （7.8 的指标来自 `runs` / `spans` 的 SQL 聚合，7.9 的 Demo 只是内容）。

## 4. 一次对话（Chat SSE，含工具与预检索）

```text
POST /conversations/{id}/messages  (Accept: text/event-stream)
  │  services/chat_service 建 Run（kind=chat）→ Tracer.start_run
  ├─① run.started
  ├─② 预检索（agent.knowledge_bases 非空时，4.4.2 模式 a）→ retrieval.completed + retriever span
  ├─③ agent.step.started → message.started → message.delta… → message.completed
  ├─④ 模型要工具 → tool.call.started → tool.call.completed ｜ tool.call.failed（回填 JSON，不中断）
  │    → agent.step.completed → 回到 ③（≤ agent.max_steps）
  ├─⑤ usage.updated（token / cost）
  └─⑥ run.completed ｜ run.failed ｜ error，最后 done（心跳 heartbeat 每 15s）
```

- **Backlog 事件**（定义在 `core/events.py`，MVP 不产生）：`approval.required`（SD-17）、
  `memory.recalled` / `memory.updated`（SD-15）。
- **Workflow 的内联形态**：`agent.workflow_id` 非空时，Chat 的这次 Run 会派生 `workflow.*`
  节点事件并复用同一份 `trace_id`（4.4.4；Demo 1 走的就是这条路径，见 `phase-8-demo.md`）。
- **Trace 示例**（一条 4 step 的 Run，可在 `/traces/{trace_id}` 折叠展开）：

```text
run(chat, 2.4s)
├── retriever  kb=product-docs  hits=3  chunk_ids=[…]      （②）
├── agent.step[1]
│   ├── llm    model=qwen-plus  prompt_tokens=812  completion_tokens=96
│   ├── tool   name=calculator  status=ok
│   └── llm    model=qwen-plus  （工具结果回填后的第二轮）
└── agent.step[2]
    └── llm    model=qwen-plus
```

## 5. 三张图分别在哪

| 想看的图 | 文件 |
|---|---|
| 进程启动、请求信封、`/readyz` | `phase-0-bootstrap.md` |
| 一轮对话时序、Trace 层级、Run 状态机 | `phase-1-agent-runtime.md` |
| 工具 9 步流水线、权限与开关 | `phase-2-tool-calling.md` |
| Workflow 手动运行、节点失败策略、checkpoint/resume、Chat 内联 | `phase-3-workflow.md` |
| 摄取链路、两条检索入口、引用格式 | `phase-5-rag.md` |
| 评测执行 → 落库 → 断言、指标看板、Trace 树 | `phase-7-eval.md` |
| 三个 Demo 的链路与装载顺序（Phase 8 收口） | `phase-8-demo.md` |
