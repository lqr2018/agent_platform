# Agent Platform —— 配置驱动的 LLM Agent 编排与执行平台

一个「配置驱动」的 Agent 平台：用配置定义 Agent（模型 / Prompt / 工具 / 知识库 / 工作流），
平台负责**执行、编排、可观测**，把每一次运行的全链路（Run → Agent → LLM / Tool / Retriever）落成可查询的 Trace。

文档是唯一事实来源：

| 文档 | 作用 |
|---|---|
| [`docs/设计大纲.md`](docs/设计大纲.md) | 需求与总体架构（Why / What） |
| [`docs/详细设计.md`](docs/详细设计.md) | 实现契约：目录、数据模型、API、运行时、测试、实施计划（How） |
| [`docs/diagrams/`](docs/diagrams/) | 各阶段时序与链路图（总架构 `architecture.md`、Demo 链路 `phase-8-demo.md` 等） |

---

## 1. 这是什么 / 不是什么

**是**：一个可本地一键起、能当场演示「配置一个 Agent → 带工具与知识库跑起来 → 看到完整 Trace」的平台；
每个模块都有清晰接口、可替换实现（Provider 抽象）与测试。

**不是**（第一阶段明确不做，见《详细设计》0.4.2 Non-Goals）：不做多租户与鉴权、不做并行 Workflow 分支、
不做人工审批后台、不做 MCP、不做长期记忆、不做评测平台 UI、不做分布式部署（单进程 + SQLite）。

### 1.1 MVP 已交付的五件套

| # | 能力 | 配置入口 | 复现路径（详见 §4） |
|---|---|---|---|
| 1 | Agent 配置 | `/agents` 或 `POST /api/v1/agents`（模型 / Prompt / 工具 / KB / Workflow / 循环上限） | `/agents` → 打开任一 Agent 看声明式配置 |
| 2 | Tool Calling | `configs/agents/*.yaml` 的 `tools`；权限与启停在 `/tools` | `/chat` 提问「算一下 (120+30)*4」→ 工具调用卡片 |
| 3 | Workflow | 图定义（JSON/YAML）或 `configs/workflows/*.yaml`；Agent 用 `workflow_id` 绑定 | `/workflows` → 运行 → 节点状态逐个变绿 |
| 4 | RAG | Agent 的 `knowledge_base_ids`（按需检索工具 `kb_search` 属迭代 E） | `/knowledge` 上传 → `/chat` 提问带 `[1]` 引用 |
| 5 | Trace / 评测 | 无需配置；`configs/eval/*.yaml` 是用例集 | `/traces` 看 span 树、首页看指标、`evaluate.py` 出报告 |

**0.5.5 的 8 步演示**（能一次跑通 = MVP 达标）见 §2.4；`.\scripts\demo.ps1` 装载完会把这份路径打印出来。

### 1.2 尚未实现、但设计已就位（迭代 A–E）

Memory（长期记忆 / 跨会话）、MCP（远程工具）、人工审批后台、评测平台（`eval_*` 四表 + Evaluation 页面与历史对比）、
`kb_search` 按需检索工具、`PdfLoader` / `WebLoader` / `Reranker`、Workflow 并行分支、多用户与鉴权、
PostgreSQL / 第二套向量库。

**这些既没有接口也没有空页面**（SD-14②：不建空壳）。不做的理由，以及"什么信号下值得重开"，
写在《详细设计》0.4.2 与 10.3；`/api/v1/meta` 的 `features` 会如实回报为 `false`。

---

## 2. 30 秒快速开始

### 2.1 容器方式（一条命令起全部）

```bash
docker compose -f docker/docker-compose.yml up --build   # 前端 http://localhost:8080，api http://localhost:8000/docs
```

数据库 / 向量库 / 文件沙箱 / 评测报告全落在 `backend/data` 一个卷里；`DATABASE_URL`、`VECTOR_STORE_KIND`
保持 `Settings` 默认值，不需要额外配置。

### 2.2 配置模型 Provider

平台**不内置任何模型 Key**，两种方式任选：

- 环境变量：把 [`.env.example`](.env.example) 拷成 `.env`（容器方式放 `docker/.env`），填
  `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `DEFAULT_MODEL`；
- UI：打开 `/models` 新建 Provider（`api_key` 用 Fernet 加密落库、列表掩码显示），点「测试」确认连通。

### 2.3 装载三个 Demo Agent

```powershell
.\scripts\demo.ps1                                          # 迁移 → 就绪检查 → seed_demo → 打印 8 步体验路径
.\scripts\demo.ps1 -Docs -Reset                              # 干净重放，并把演示语料灌进知识库
.\scripts\demo.ps1 -Provider my-provider -Model qwen-plus     # 指定 Provider / 模型
```

容器里等价于：

```bash
docker compose -f docker/docker-compose.yml exec api python -m app.scripts.seed_demo --docs
```

装载**幂等**（按 `name` upsert，`--reset` 先删同名再建）；Demo 数据只有 `seed_demo` 这一条通道（理由见 §5.4）。

### 2.4 打开 8080，走 0.5.5 的 8 步

| 步 | 去哪 | 做什么 |
|---|---|---|
| 1 | 首页 | 确认 `/readyz` 为 ok（DB 可写 + 迁移在 head + 向量库可达） |
| 2 | `/agents` | 打开 Research Agent：Prompt + 工具 + 知识库 + Workflow + `max_steps` |
| 3 | `/chat` | 选 Data Analysis Agent，问「帮我算一下 12 万的 15% 是多少，并读取 docs/spec.md 的第一段」→ 流式增量 + 工具调用卡片 |
| 4 | `/traces` | 展开该 Run 的 span 树（run → agent → llm → tool → llm） |
| 5 | `/workflows` | 打开 research-flow 看只读图 → 运行 → 节点逐个变绿（`node_runs`） |
| 6 | `/chat` | 选 Knowledge Assistant，问「我们产品的部署方式有哪些？」→ 答案带 `[1]` 引用与来源卡片 |
| 7 | 终端 | `evaluate.py --suite demo-suite --agent research-agent` → 报告落在 `backend/data/reports/` |
| 8 | 本文 | 架构（§3）+ 评测结果（§6）+ 已知限制与再评估触发条件（§8） |

本机（不用容器）的起法与质量门禁见 §7.1；一键脚本是 `.\scripts\dev.ps1`。

---

## 3. 架构

### 3.1 分层与依赖方向

```text
frontend  →  /api/v1 (HTTP + SSE)  →  api  →  services  →  db/models
                                              ↘  runtime/*  →  runtime/observability
```

- **api**：协议转换、参数校验、组装 DTO；
- **services**：事务边界、实体 CRUD、跨实体编排（唯一写库者）；
- **runtime**：纯执行逻辑，**不 import services/api**，通过入参 dataclass 拿到数据 → 可单测、可脱离 DB 运行；
- 每个可替换家族（LLM / Embedding / VectorStore / Memory / WorkflowEngine / Tool）都是 `base.py` 抽象 + `registry.py` 按 `kind` 实例化；
- 完整分层图、运行时组件表与依赖方向：`docs/diagrams/architecture.md` §1 / §2。

### 3.2 运行时组件

| 组件 | 职责 |
|---|---|
| `AgentRuntime` | 消息 + 工具 + 知识库的一次循环；`max_steps` / 超时 / 取消的收敛点（4.4.3） |
| `Retriever` | 固定预检索与工具式检索共用一个实现（4.6.3） |
| `ToolExecutor` | 九步流水线：查表 → 校验 → 权限 → 沙箱 → 执行 → 落库（4.2.3） |
| `SimpleEngine` | 串行图、条件路由、checkpoint / resume（SD-10） |
| `Tracer` | Run → agent → llm / tool / retriever 的 span 树与 `trace_id` 传播（4.8） |
| `TaskRunner` / `EvalRunner` | 进程内队列（摄取 / 评测）、用例 → 断言 → 指标 → 报告（4.6.2 / 4.9） |

### 3.3 数据模型（16 张表）

- 配置侧：`model_providers` / `agents` / `agent_prompt_versions` / `tools` / `workflows`；
- 运行侧：`conversations` / `messages` / `runs` / `traces` / `spans` / `tool_invocations` / `workflow_runs` / `node_runs`；
- 知识侧：`knowledge_bases` / `documents` / `chunks`（向量存在 Chroma）。

判据唯一：Run 的状态只看 `runs.status`，文档是否可检索只看 `documents.status`（4.6.2）。
Backlog 的 `memory_items` / `mcp_servers` / `eval_*` **不建表**（SD-14②）；ER 图见 `docs/diagrams/architecture.md` §3。
迁移链：`0001_phase0_baseline` → `0005_phase5_knowledge_tables`，Phase 7 / 8 **无新增表**。

### 3.4 一次对话（SSE 事件流）

```text
run.started → retrieval.completed（绑了 KB 时）→ agent.step.started → message.started / delta / completed
           → tool.call.started / completed | failed（模型要工具时）→ agent.step.completed（回到上一步重来）
           → usage.updated → run.completed | run.failed | error → done （每 15s 心跳）
```

事件名单点定义在 `core/events.py`（21 个，含 3 个 Backlog 事件），与文档 3.4 的一致性由 `test_sse_contract.py` 钉住；
Chat 内联 Workflow 时同一条 SSE 里出现 `workflow.node.started/completed` 并复用同一个 `trace_id`（4.4.4）。
时序图与 Trace 层级示例见 `docs/diagrams/architecture.md` §4。

---

## 4. 平台能力清单（六项，每项：配置方式 + 复现步骤）

| 能力 | 阶段 | 状态 |
|---|---|---|
| 基础设施：配置 / 日志 / 错误模型 / 迁移 / 健康检查 | Phase 0 | ✅ |
| Agent Runtime：Provider / Agent CRUD / 流式对话 / Trace | Phase 1（M1） | ✅ |
| Tool Calling：Tool Registry / 权限分级 / 沙箱 | Phase 2（M2） | ✅ |
| Workflow：串行图 / 状态落库 / 断点续跑 | Phase 3（M2） | ✅ |
| RAG：知识库 / 摄取 / 检索 / 引用 | Phase 5（M3） | ✅ 后端 + 知识库页 + Chat 引用来源卡片 |
| 评测脚本 + 指标看板 + Trace 树 | Phase 7 + Phase 8 收口 | ✅ CLI / 契约用例集 / 六项指标 / **`/stats` 看板** / 服务端建树；评测平台页面（逐用例 `detail` + 历史对比）属**迭代 D** |
| Memory / MCP / 审批后台 / 评测平台 | Backlog | 设计已就位，**不提供接口、不建空页面**（0.5.3 / SD-14②） |

### 4.1 Agent 配置

- **配置方式**：`POST /api/v1/agents` 或 `/agents` 页；批量落地用 `configs/agents/*.yaml`（§2.3）。
- **复现**：`/agents` → 打开 Knowledge Assistant，可见 Prompt / 模型 / 工具 / 知识库 / `max_steps`；
  改 Prompt 会留档到 `agent_prompt_versions`（回滚端点属延后项，7.0.1）。

### 4.2 Tool Calling

- **配置方式**：Agent 的 `tools`（按 name / id）；权限分级与启停在 `/tools`；`python_execute` / `file_write`
  需在 `Settings` 显式开启（`PYTHON_EXECUTE_ENABLED` / `FILE_WRITE_ENABLED`，SD-17）。
- **复现**：`/chat` 问「帮我计算 (120+30)*4」→ 看到 `tool.call.started/completed` 与工具卡片；
  `/traces` 里该 Run 有 tool 层 span；`tool_invocations` 行带 `permission_decision`。

### 4.3 Workflow

- **配置方式**：`/workflows` 编辑图，或 `POST /api/v1/workflows`；Agent 用 `workflow_id` 绑定
  （绑定后 Chat 里内联执行，4.4.4）。
- **复现**：`/workflows` → research-flow → 校验通过 → 运行（202）→ 页面 2s 轮询 `node-runs` 节点逐个变绿；
  运行中可 `cancel`，失败可 `resume`（两类合法场景见 4.5.4）。

### 4.4 知识库（RAG）

- **配置方式**：`/knowledge` 新建 KB（embedding provider / 切片器 / `top_k` / `score_threshold`）→
  上传 `.md` / `.txt` → 文档走到 `ready`。
- **复现**：绑了 KB 的 Agent 在 Chat 里提问 → 命中后答案带 `[1]` 与引用来源卡片；未命中时明确说明
  "未检索到相关内容"而不是编造；`GET /maintenance/knowledge-bases/{id}/verify-index` 可对账 SQLite 与向量库。

### 4.5 Trace

- **配置方式**：无（全链路默认落库）。
- **复现**：`/traces` → 任一 Run → span 树（`run → agent → llm / tool / retriever`；Workflow 再加
  `workflow → node:{id}` 层）；`GET /api/v1/traces/{trace_id}/tree` 是服务端建树版，
  `truncated=true` 表示超出上限没取全。

### 4.6 评测（脚本）

- **配置方式**：`configs/eval/{contract-suite,demo-suite}.yaml`（YAML 是用例源，SD-18）；
  断言类型 7 种（`contains` / `regex` / `equals` / `json_path` / `tool_sequence` / `max_latency` / `llm_judge`）。
- **复现**：`python -m app.scripts.evaluate --suite demo-suite --agent research-agent`；
  报告落 `backend/data/reports/eval-*.md|json`，失败用例带 `trace_id` 可直接跳 `/traces`（§6）。

---

## 5. 完成度与路线图

> 对应《详细设计》附录 D 的"路线图与完成度"一节；这里把它放在能力清单之后（附录 D 排在 §9），内容不变。
> 三个 Demo 的细节见 §6，评测结果见 §7。

**已完成：Phase 0 项目基础设施 + Phase 1 最小 Agent Runtime + Phase 2 Tool Calling + Phase 3 Workflow + Phase 5 RAG（后端 + 前端）+ Phase 7 的评测脚本链路与指标 / Trace 树接口 + Phase 8 MVP 收口（三个 Demo Agent + `seed_demo` / `demo.ps1` + README + `/stats` 看板）**（对齐《详细设计》7.1 / 7.2 / 7.3 / 7.4 / 7.6 / 7.8 / 7.9）

Phase 0（基础设施）：

- [x] `Settings` 单一配置入口（字段与附录 C 全量对应）+ `structlog` 日志（dev 彩色 / prod JSON）+ 密钥脱敏
- [x] `AppError` 错误码体系 + 全局异常处理器 + `{data, meta}` / `{error, meta}` 统一响应封装
- [x] `RequestIdMiddleware`（纯 ASGI，SSE 安全）：注入 `request_id` 上下文、回 `X-Request-Id` 响应头、访问日志
- [x] `db/base.py`（`TimestampMixin` / `ULIDStr` / `JSONDict`）+ async engine + `get_session`
- [x] Alembic（`render_as_batch=True`）+ `upgrade head` / `downgrade base` 往返（含 SQLite batch 模式验证测试）
- [x] `/healthz`、`/readyz`、`/api/v1/meta`
- [x] `docker-compose.yml` + `nginx.conf`、`scripts/{dev,migrate}.ps1`、`.github/workflows/ci.yml`

Phase 1（最小 Agent Runtime）：

- [x] 8 张表迁移 `0002_phase1_core_tables`（`model_providers` / `agents` / `agent_prompt_versions` / `conversations` / `messages` / `runs` / `traces` / `spans`）
- [x] LLM 层：`LLMProvider` 协议、OpenAI-compatible 实现（流式 + `tool_calls` 增量拼接 + 重试）、`FakeLLMProvider`（测试替身）、按 `kind` 实例化的 `LLMRegistry`
- [x] Agent Runtime 主循环（无工具分支）：上下文装配（system → 短期窗口 → 本轮消息）、停止条件、取消、超时、`usage` 累加
- [x] Tracer 落库：`run → agent → llm` 三层 span + 每层耗时/token/成本，`spans` / `traces` 汇总
- [x] API：`model-providers`（含 `/test`、`api_key` 加密与掩码）、`agents`（含 clone、Prompt 版本留档）、`conversations`、`runs`、`traces` / `spans`
- [x] Chat SSE（`run.started → message.* → usage.updated → run.completed/failed → heartbeat/done`）+ 同会话并发保护 + `POST /runs/{id}/cancel`
- [x] 前端：`/agents[/:id]`、`/models`、`/chat[/:id]`（流式渲染 + Run 状态卡）、`/traces[/:traceId]`（span 树 + 明细抽屉）
- [x] 契约测试：3.4 事件名与 payload ↔ `core/events.py` ↔ `types/events.ts`；迁移 ↔ ORM 一致

Phase 2（Tool Calling）：

- [x] 迁移 `0003_phase2_tool_tables`（`tools` / `tool_invocations`）+ 数据迁移写入 5 个内置工具（`calculator` / `file_read` / `file_write` / `web_search` / `python_execute`）
- [x] Tool 运行时：`runtime/tools/{base,registry,executor,permissions,sandbox}.py` + 5 个内置工具（`calculator` AST 白名单、`file_write` / `python_execute` 需显式开启、`web_search` 无 Key 降级）
- [x] **九步流水线**：查表 → 合并权限 → 参数校验（自研 JSON Schema 子集）→ 权限判定 → 次数闸门 → 执行（限流 + 超时 + builtin/api 分派）→ 输出截断 → 落库（`tool_invocations` + tool span）→ 返回；失败不中断 Run（错误回填给模型，连续 3 次才终止）
- [x] `AgentRuntime` 循环接入 `tool_calls` 分支：每步重算可见工具、`role=tool` 消息落库、`max_steps` / 重复失败停止条件
- [x] API：`/tools` 全量 7 端点（读 3 + **写 4**：`POST` / `PATCH` / `DELETE` / `POST /{id}/test`）+ `GET /tool-invocations`
- [x] 前端：`/tools` 工具管理页（列表 / 筛选 / 详情 / 启停 / 权限编辑 / 试跑 / 删除）+ Chat 内**工具调用卡片**（名称 / 参数 / 结果 / 耗时 / 状态）+ Trace 树 tool 层（`input` / `output`）
- [x] 启动对齐：`sync_builtin_tools()` 把代码侧定义同步进 DB，且**不动**运营侧的 `status` / `permission_config`

Phase 3（Workflow）：

- [x] 迁移 `0004_phase3_workflow_tables`（`workflows` / `workflow_runs` / `node_runs`）+ 补 `agents.workflow_id` / `runs.workflow_run_id` 两个外键（batch 迁移，`ON DELETE SET NULL`）
- [x] 图解析与静态校验（`runtime/workflow/graph.py`）：唯一 id / 一个 start / 至少一个 end / 可达 / 无孤立节点 / 引用存在，**多出边 → `PARALLEL_EDGES_NOT_SUPPORTED`**（SD-1）；模板保存时静态检查（白名单外函数 / 未知根）
- [x] 受限模板求值（`template.py`）：`{{state.x}}` / `{{nodes.<id>.output}}` / `{{run.run_id}}` + 白名单函数（`len` / `str` / `join` / `json.dumps`），**不使用 `eval`**；未定义字段 → 空串 + warning
- [x] 六类节点语义（`nodes.py`，无 `human`）：`start` / `agent` / `tool` / `retriever` / `condition` / `end`；`on_error` 三态 `fail` / `continue` / `retry(n)`；`max_steps` + `recursion_limit` 双重上限
- [x] `SimpleEngine`（唯一引擎，SD-10）+ `WorkflowEngine` Protocol + conformance 模板（`agent` / `tool`节点复用既有 Runtime / 九步流水线）
- [x] 每节点落 `node_runs`（`seq` / `iteration` / `attempt` / 状态 / 耗时 / 输出摘要）并更新 `state` / `current_node_id` / `checkpoint`；span 层级 `run → workflow → node:{id} → agent/tool/llm`
- [x] `checkpoint` + `resume`（两类合法场景：可重试失败的 Run、进程重启后的孤儿 Run）；引擎版本不匹配按 SD-10 降级从头重跑
- [x] API：`/workflows` 全量 7 端点（含 `/validate` 与 `/publish`）+ `/workflow-runs` 5 端点（列表 / 详情 / `node-runs` / `resume` / `cancel`）
- [x] Chat 内联 Workflow（4.4.4）：`agent.workflow_id` 非空时由引擎驱动，事件 13/14 与消息事件走同一条 SSE
- [x] 启动自检第 3 条：孤儿 Run / WorkflowRun 收敛（`RUN_ABANDONED`）
- [x] 前端：`/workflows[/:id]`（列表 / JSON 编辑 / 校验面板 / 只读图 / 试跑 + 2s 轮询节点状态 / resume / cancel / 历史运行）
- [x] `configs/workflows/*.yaml`（8.2 / 8.4 的示例图）+ 装载器与结构校验

Phase 5（RAG 知识库，**先于 Phase 4 落地**，见《详细设计》附录 F v1.15 / v1.16）：

- [x] 迁移 `0005_phase5_knowledge_tables`（`knowledge_bases` / `documents` / `chunks`，含 `kb_id` / `(kb_id, checksum)` 等索引）
- [x] RAG 运行时（`runtime/rag/**` 七个平铺模块）：`TextLoader` / `MarkdownLoader`、`RecursiveSplitter` / `MarkdownSplitter`、`OpenAICompatibleEmbedder` / `FakeEmbedder`（+ 建 KB 时的维度探测）、`ChromaVectorStore` / `InMemoryVectorStore`、`Retriever`、`IngestPipeline`
- [x] 摄取流水线（4.6.2 状态机）：`pending → parsing → chunking → embedding → ready|failed`；checksum 幂等（同文件二次上传**不再 embed**）、`reingest`、进程内 `TaskRunner`（停止时排空 / 启动时收敛中间态）
- [x] API：`/knowledge-bases` 12 端点（CRUD / multipart 上传 / 文档列表与详情 / 切片预览 / `POST /query` / `reingest` / 删除）+ `GET /maintenance/knowledge-bases/{id}/verify-index` 对账
- [x] Chat 接入（4.4.2 模式 a）：绑定 KB 的 Agent 在**首轮 LLM 之前**做固定预检索 → system 注入 `<chunk id source>` 块 → 发 `retrieval.completed`（事件 10）+ `retriever` span；未命中 / 检索异常只追加"未检索到相关内容"提示，**不打断对话**
- [x] Workflow 接入（4.5.2）：`retriever` 节点复用同一套检索，命中切片（带 `source`）写进 `output_key`；`kb_id` 不存在 → `KB_NOT_FOUND`，按节点 `on_error=continue` 记错误码后继续
- [x] `/readyz` 的 `vector_store` 检查改为真实探测（chroma `heartbeat()` / memory 恒 ok）
- [x] 文档：`docs/diagrams/phase-5-rag.md`（附录 F 规则 3）+ 1.1.1 落点更新
- [x] **前端**：`api/kb.ts`（12 端点 + `verify-index` 对账）+ `/knowledge` 知识库页（列表 / 新建与编辑 / 上传 multipart / 文档表 2s 进度轮询 / 切片预览弹窗 / 检索试算多 KB）+ Chat 引用来源卡片（`components/chat/CitationCard.tsx` 消费事件 10）+ Agent 编辑页的 `knowledge_base_ids` 绑定入口

Phase 7（评测脚本链路 + 指标看板 + 服务端 Trace 树；**未做项在块尾单独列出**，落点与偏差见《详细设计》附录 F v1.19 / v1.20）：

- [x] 运行时 `app/runtime/eval/`：6 类确定性断言（`contains` / `regex` / `equals` / `json_path` / `tool_sequence`（`exact` / `contains` / `in_order` 三种 mode）/ `max_latency`）+ `llm_judge`（强制 JSON、阈值判定、成本计入）；断言**全部执行、不短路**，失败项带 `detail`（期望 vs 实际）
- [x] `EvalRunner` + `YamlSuiteSource`：用例来自 `configs/eval/*.yaml`（取代 DB 用例表）、用例级并发上限、单 case 超时、三终态 `passed` / `failed` / `error`（**执行异常**与**断言求值不了**都归位 `error`，与"断言没过"区分开）
- [x] 指标（4.9.2 口径）：`task_success_rate` / `tool_success_rate` / `workflow_success_rate`（无 workflow 用例时为 `null`）/ 平均与 P95 延迟 / 平均工具调用 / 平均 token / 成本 / 错误率 / judge 分数分布
- [x] 报告：大纲式文本 + `data/reports/eval-*.md|json` 落盘（`REPORT_KEEP` 轮转），每条失败用例附 `trace_id`
- [x] `python -m app.scripts.evaluate`：`--suite` / `--agent` / `--cases` / `--concurrency` / `--judge` / `--output` / `--json` / `--timeout`；`--json` **只**打结构化结果（与落盘同源），退出码 = `failed + errors`，配置/环境问题 → `2`
- [x] 接线真实运行时（4.9.1）：每个用例 = 独立 conversation + 一条 `runs.kind=eval`（`trace_id` 可在 `/traces` 定位），预检索复用 `chat_service.retrieve_context`，Agent 带 `workflow_id` 时由引擎驱动
- [x] 用例集：`configs/eval/contract-suite.yaml`（离线契约，`FakeLLMProvider`，5 条启用）、`configs/eval/demo-suite.yaml`（真实模型 + `llm_judge`，`research-agent`）；`task-6-kb-search`（迭代 E）与 `task-6-mcp-remote-tool`（迭代 B）为 `enabled: false` 占位，不计入分母
- [x] `GET /api/v1/stats/overview`（7.8 任务 5）：聚合 `runs` / `spans` 的六项基础指标 —— 成功率 / 平均时延 / 平均工具调用 / 平均 token / 错误率 / 成本（外加 tool span 成功率与状态计数）；字段名与 4.9.2 的报告**共用一套**，`?from=&to=` 与 `/traces` 同窗口，`?kind=` 可把评测批次排除在平台指标外
- [x] `GET /api/v1/traces/{trace_id}/tree`（7.8 任务 6）：服务端按 `parent_span_id` 建嵌套树（`run → agent / workflow → llm / tool / retriever`）；孤儿 / 自环当根、兄弟按 `seq` 升序、**显式栈不递归**（1000 深链也不爆栈）；树不分页，超上限或有 span 不可达时 `truncated=true`
- [x] `spans` 分页（同任务 6）：`GET /traces/{id}?limit=&cursor=` 键集分页（游标 = 上一页最后一个 `seq`，`meta.next_cursor` 沿用 3.1 统一信封）；**不传 `limit` 时行为与 Phase 1 一致**（返回全部 span），故前端零改动
- [x] 契约同步：`docs/openapi.json`（**41 → 43 paths**）与 `frontend/src/types/api.d.ts` 重导（3.5 / 9.3 的"生成结果与提交版本一致"门禁）
- [x] 文档：`docs/diagrams/phase-7-eval.md`（附录 F 规则 3）+ 附录 F v1.19 / v1.20 + 7.8 DoD 勾选（**1–4 条**；第 5 条 Evaluation 页面属迭代 D）
- [ ] **未做（不假装完成）**：`/stats` 看板（并入 Phase 8 的前端收口）；`eval_*` 四张表、`DbSuiteSource`、`eval_api.py`、Evaluation 页面与历史对比属**迭代 D**；`llm_judge` 只挂在 `demo-suite` 的手工路径上，**从未对真实模型跑过**

Phase 8（MVP 收口：三个 Demo Agent + 装载脚本 + README/架构图 + `/stats` 看板；落点与偏差见《详细设计》附录 F v1.21，装载脚本的稳健性加固见 v1.22）：

- [x] 三个 Demo 的配置：`configs/agents/{research-agent,data-analysis-agent,knowledge-assistant}.yaml`（`AgentConfigFile` 强校验）+ 可选图 `configs/workflows/research-flow.yaml`；`research-flow` / `kb-qa-flow` 的入参键统一为 `input`
- [x] `python -m app.scripts.seed_demo`：工具对齐 → Provider 解析（**每个 Agent 用自己 YAML 里的 `model.provider`**，`--provider` 传了才统一覆盖）→ KB（按 `name` 建或复用）→ Agent 第一遍 → Workflow 名 → id 解析并发布 → Agent 第二遍回填 `workflow_id` → `--docs` 灌语料；`--reset` **硬删**同名 Agent / Workflow（SQLite 没开 FK 级联，脚本逐表显式删 —— 不留悬空行；配置外的别人的 Run / 绑定只**置空指针**，不删行），退出码 `2` = 环境 / 配置问题（库未迁移、配置写错或重名、provider / tool 名字写错、embedding 不可用，报错带文件名而不是裸 traceback）（**没有 `0009` 迁移**：数据模型变更 = 无，Demo 是内容不是 schema）
- [x] `scripts/demo.ps1`：`alembic upgrade head` → `check_readyz` → `seed_demo` → 探 `/healthz` → 打印 0.5.5 的 8 步路径（`-Docs` / `-Reset` / `-Provider` / `-Model`）；**已在 Windows PowerShell 5.1 下实测**：本机库没有 Provider 时退出码 `2` 并打印"先在 /models 建一个"的指引（不打印假成功），隔离临时库里的 `seed_demo --docs --reset` 全流程退出码 `0`
- [x] `scripts/*.ps1` 统一存成**带 BOM 的 UTF-8**（修掉一个长期潜伏的问题：PS 5.1 把无 BOM 脚本按 GBK 解码，`dev.ps1` / `demo.ps1` 会因中文乱码吞掉引号而直接语法报错）
- [x] `/stats` 看板（`src/api/stats.ts` + 重写的 `pages/DashboardPage.tsx`）：六项指标 + 窗口 / `kind` 选择器，空窗口显示 `-` 而不是 `0`，**只做数字不做图表**（SD-14）
- [x] 链路与评测证据：`tests/e2e/test_demo_agents.py`（三条链路）、`tests/integration/test_seed_demo.py`、`tests/unit/test_demo_genericity.py`、`tests/unit/test_agent_configs.py`；`configs/eval/demo-suite.yaml` 去掉不存在的 `kb_search` 断言
- [x] 文档：README 按**附录 D 大纲**重写（§1–§10）+ `docs/diagrams/architecture.md`（分层 / 16 张表 / 一次会话 SSE）+ `docs/diagrams/phase-8-demo.md`（三条链路、装载顺序、8.5 验收落地）
- [ ] **未做（不假装完成）**：`docker compose up --build` 的 10 分钟端到端走查（本机无 Docker，CI 只做镜像构建）；`demo-suite` 的真实模型路径与 `llm_judge` 实测（需自备 Key）；§9 的迭代 A–E 内容

**验证情况（Phase 5 后端）**：`pytest` 全量通过（unit + 19 个 integration 文件）、`ruff check` / `ruff format --check` / `mypy app` 0 告警、`alembic check` 干净（head = `0005_phase5_knowledge_tables`）、`python -m app.scripts.check_readyz` 通过、`export_openapi` 幂等（`frontend/src/types/api.d.ts` 已随之重导）。知识库端到端在 `tests/integration/test_knowledge_api.py`：上传 → `ready` → 命中（带 `source`）→ 删除后不再命中、同 checksum 不重复 embed（断言 embedding 调用次数）、`verify-index` 两端计数一致、Chat 预检索的事件 10 与 `<chunk>` 注入（含未命中与未绑 KB 两个分支）、Workflow `retriever` 节点的命中与 `KB_NOT_FOUND` 分支。

**验证情况（Phase 5 前端）**：`npm run lint`（eslint 0 告警）/ `prettier --check` / `tsc --noEmit` / `vitest`（**47 项**，10 个文件）/ `vite build`（3077 modules，`dist/assets/index-*.js` 1.35 MB，gzip 424 kB）全绿。新增用例：`src/api/__tests__/kb.test.ts`（12 个端点的 URL / 方法 / body，特别钉住 multipart **不手写** `Content-Type`、`/query` 的多库 `kb_ids`、`verify-index` 走 `/maintenance`）、`src/components/chat/CitationCard.test.tsx`（未命中 / 明细在拉 / 明细已到 / 拉取失败四态）、`src/pages/__tests__/KnowledgePage.test.tsx`（列表口径与空态）、`src/stores/chatStore.test.ts` 的事件 10 三条（骨架与补拉、未命中即终结、无检索轮次不清空）。前端落点见《详细设计》附录 F v1.18（含"事件 10 只带计数、切片正文按需补拉"的口径）。

**验证情况（Phase 7：评测脚本链路 + 指标 / Trace 树）**：`pytest` **629 项**（625 passed + 4 skipped）、覆盖率 **89.89%**（`TOTAL 9462 957 90%`，门槛 `--cov-fail-under=70`）、`ruff check` / `ruff format --check .`（198 files）/ `mypy app`（124 source files）0 告警、`alembic check` 干净（`No new upgrade operations detected`；本阶段**无新增表**）、`python -m app.scripts.check_readyz` 通过（`ok=True revision 0005_phase5_knowledge_tables`）、`export_openapi` / `gen:api` 重导后与提交版本一致（`docs/openapi.json` **41 → 43 paths**，已随本轮提交）。新增用例 65 项（52 + 本轮 13）：`tests/unit/test_eval_assertions.py`（20）/ `test_eval_runner.py`（15）/ `test_eval_metrics.py`（8）/ `test_eval_judges.py`（5）+ `tests/integration/test_eval_contract_suite.py`（4）。契约套件的端到端证据（`test_eval_contract_suite.py`，真实 `create_app()` + lifespan + `FakeLLMProvider` + 内存向量库）：`configs/eval/contract-suite.yaml` 的 5 条启用用例全 `passed`（`task_success_rate=1.0`、`tool_success_rate=0.75`、`workflow_success_rate=null`、`avg_tool_calls=0.8`），5 条 `runs.kind=eval` 全 `succeeded` 且各自独立会话，4 行 `tool_invocations` 中 `task-4-tool-error-backfill` 那行 `failed` / `TOOL_EXECUTION_FAILED`（证明"工具失败回填给模型、Run 仍成功"），每个 Run 恰好 1 个 `retriever` span（`strategy=prefetch`、`hit_count>=1`、`output.chunk_ids` 非空 —— 预检索真的进了 system）；CLI 侧（真子进程跑 `python -m app.scripts.evaluate`）钉住 `--json` 只打 JSON、stdout 与落盘 `.json` 同源、失败用例退出码 = `failed + errors` 且带 `trace_id`、`--cases` 指定 `enabled: false` 的占位用例 → `CONFIG_INVALID` + 退出码 `2` 且不落半份报告。本轮（v1.20）的指标与 Trace 树侧：`tests/integration/test_stats_api.py`（4 项）钉住"真实 chat Run 的六项指标"（`FakeLLMProvider` 的 `cost_usd=0` 也照实断言，不假装有成本）与"造行后 `success_rate=0.5` / `error_rate=0.5` / 均值只算收敛行 / `tool_success_rate=0.5`"，空窗口返回 `null` 而不是 `0.0`，`?kind=` 可把评测批次排除；`tests/unit/test_trace_tree.py`（6 项）钉住建树的五条边界（孤儿 / 自环当根、兄弟按 `seq`、环丢弃、上限截断、1000 深链不撞递归上限）；`test_runs_and_traces_api.py`（+3 项）钉住 `run → agent → llm` 三层嵌套、未知 `trace_id` 的 404、spans 键集分页（`limit=2` → `next_cursor="2"` → 末页 `next_cursor=null`）与非法游标 422。

**验证情况（Phase 8：MVP 收口）**：后端 `pytest` **658 项**（`654 passed, 4 skipped in 163.65s`，`addopts` 带 `-m "not live"`）、覆盖率 **90.03%**（`TOTAL 9581 955 90%`，门槛 `--cov-fail-under=70`）、`ruff check app tests configs` → `All checks passed!`、`ruff format --check .`（206 files）/ `mypy app`（126 source files）0 告警、`alembic check` → `No new upgrade operations detected`（本阶段**无新增表**）、`python -m app.scripts.check_readyz` → `ok=True revision 0005_phase5_knowledge_tables`；前端 `eslint` / `prettier --check` / `tsc --noEmit` 0 告警、`vitest` **49 项（11 个文件）**全绿、`vite build` 成功（7.23s，`dist/assets/index-*.js` 1,361.56 kB / gzip 426.83 kB）、`npm run gen:api` 后 `git diff --exit-code src/types/api.d.ts` **无 diff**（OpenAPI 与前端类型一致，CI 的 `gen-api` 门禁）。本轮新增后端 21 项：`tests/e2e/test_demo_agents.py`（3：Chat 内联 Workflow / 工具循环 / 预检索 + 引用）、`tests/integration/test_seed_demo.py`（**15**：幂等装载、`--reset`、`--docs` 摄取、`name → id` 解析、配置错误退出码、坏配置文件带文件名、同名配置、per-Agent Provider、`--reset` 无悬空引用）、`tests/unit/test_agent_configs.py`（9：YAML → `AgentCreate` 映射）、`tests/unit/test_demo_genericity.py`（2：AST 扫描 + 反向自检）；前端新增 2 项（`DashboardPage.test.tsx`：六项指标渲染、空窗口显示 `-`）。三条链路的断言落点见 §6.4；`seed_demo` 的稳健性加固（配置错误收口 / Provider 按 Agent 解析 / `--reset` 不留悬空行）见《详细设计》附录 F v1.22。

**验证情况（Phase 0–3，基线）**：后端 `pytest` **509 项**（506 passed + 3 skipped）、覆盖率 **91%**（`TOTAL 6994 631 91%`）、`ruff check` / `ruff format --check` 0 告警、`mypy app` 通过、`alembic` 往返 + `alembic check`（`No new upgrade operations detected`）、就绪探针 `python -m app.scripts.check_readyz` 通过（`ok=True revision 0004_phase3_workflow_tables`）、`export_openapi` 幂等（连续两次同哈希）；前端 `eslint` / `prettier` / `tsc` / `vitest`（**28 项**）/ `vite build` 全绿。Phase 1/2 的回归用例全绿（无 Workflow 的 Agent 与 Chat 行为不变）；Phase 3 的端到端链路（真实 `create_app()` + lifespan + SQLite：建图 → 发布 → 202 运行 → 轮询 `node-runs` → resume/cancel/内联 Chat 流）见 `tests/integration/test_workflow_runner.py`。说明：`app/services/**` 与 `app/api/**` 的行覆盖率在本机 coverage 下会漏记"`await` 之后的尾行"（《详细设计》7.2 记录的口径），因此服务层另有直测补充（`tests/integration/test_workflow_runner.py` 直接断言落库结果与 `node_runs` 序列）。

下一步：**迭代 E**（`kb_search` 工具补齐 4.4.2 的第二种检索模式、`PdfLoader` / `WebLoader` / `Reranker`、`runtime/rag` 拆子包，见 4.4.2 / 4.6.1）与**迭代 A–D**（长期记忆 / MCP / 审批后台 / 评测平台，见 §9）；第一阶段的 M1–M3 到此收口，`m3` tag 后按 §5 的路线图推进。

---

## 6. 三个 Demo Agent

三个 Demo 的差异**只体现在 YAML**（`backend/configs/agents/*.yaml`）：平台里没有 `if agent.name == ...`
这类分支 —— `tests/unit/test_demo_genericity.py` 用 AST 扫描 `app/**` 的字符串字面量，命中即红。
链路图、装载顺序与踩过的坑见 [`docs/diagrams/phase-8-demo.md`](docs/diagrams/phase-8-demo.md)。

### 6.1 Research Agent（研究型）

- **它是什么**：绑 `research-flow`（`start → planner → search → router → reader → writer → end`）。
  在 Chat 里发一句话就**内联跑完整张图**（4.4.4）：事件流里出现 `workflow.node.*`，落库是
  `runs.kind=workflow` + `workflow_run_id` + 逐节点 `node_runs`。
- **能做什么**：多步编排、条件分支（`search` 有命中才走 `reader` 知识库检索节点）、工具调用
  （`web_search` / `calculator` / `file_read`）、结构化报告（结论 / 依据 / 不确定项）。
- **不能做什么**：不执行代码（`python_execute` 不在它的 `tools` 里）；没配 `WEB_SEARCH_PROVIDER` +
  `WEB_SEARCH_API_KEY` 时 `web_search` 调用被拒（`TOOL_DISABLED`），节点 `on_error=continue` 继续往下走；
  证据不足时按 Prompt 明确说"未找到足够依据"，不编造。
- **体验问句**：「调研一下 2026 年国内主流向量数据库的差异，并给出选型建议」。

### 6.2 Data Analysis Agent（数据 / 代码型）

- **它是什么**：不绑 Workflow 的**纯 Agent 循环**（证明"没有 Workflow 也照样能跑"），
  `tools = [python_execute, file_read, file_write, calculator]`。
- **能做什么**：读沙箱内文件、做计算、把结果汇总成 Markdown 表格；支持多轮追问。
- **不能做什么**：`python_execute` / `file_write` **默认关闭**（`PYTHON_EXECUTE_ENABLED` /
  `FILE_WRITE_ENABLED`），未开启时该次调用被拒（`TOOL_PERMISSION_DENIED`），模型应改用
  `file_read` + `calculator` 给出简化结论并说明限制；只能访问沙箱根（`DATA_DIR/files`）内的路径，
  越界会拒绝并落审计；仅标准库，靠 AST `import` 白名单约束而**不是**强制网络隔离（SD-4）。
- **体验问句**：「帮我算一下 12 万的 15% 是多少，并读取 docs/spec.md 的第一段」→ 追问「把 3 月的异常点解释一下」。

### 6.3 Knowledge Assistant（知识库问答型）

- **它是什么**：绑 `product-docs` 知识库、不绑 Workflow；走 Chat 的**固定预检索**（4.4.2 模式 a）——
  system 注入 `<chunk id source>` 块，并产生 `retrieval.completed` 事件与 `retriever` span。
- **能做什么**：只依据知识库回答、带 `[1] [2]` 引用与来源卡片、多轮追问、先结论后依据。
- **不能做什么**：跨会话记忆属**迭代 A**（MVP 不承诺）；只答知识库覆盖范围，检索为空时明确回答
  "知识库中没有检索到相关内容"；按需检索工具 `kb_search` 属**迭代 E**（MVP 不存在）。
- **体验问句**：「我们产品的部署方式有哪些？」→ 追问「那第二种方式和第一种相比有什么代价？」。

### 6.4 为什么它们证明"平台是通用的"（8.5 验收表）

| 检查 | 方法 | 结果 |
|---|---|---|
| 无专属代码 | `grep -rn "Research Agent\|Data Analysis\|Knowledge Assistant" backend/app/` | 只在注释 / docstring 里出现；`tests/unit/test_demo_genericity.py` 扫 `app/**` 的字符串字面量，命中即红 |
| 配置即创建 | 删库 → `alembic upgrade head` → `python -m app.scripts.seed_demo` | 三个 Agent 可用，无手工步骤（`tests/integration/test_seed_demo.py` 钉住引用解析） |
| 能力可组合 | 清空 Demo 1 的 `workflow` 字段 | 仍可对话（退回纯 Agent 循环），无需改代码 |
| 工具可替换 | Demo 2 把 `python_execute` 换成只读工具 | 只改 YAML |
| 契约测试 | `pytest tests/e2e/test_demo_agents.py` | 三个 Agent 各跑通一条链路（见下表） |
| 评测覆盖 | `evaluate.py` 跑 contract + demo suite | `contract-suite` 成功率 = 100%（§7.1） |

`tests/e2e/test_demo_agents.py` 的三条链路（真实 `create_app()` + lifespan + 临时 SQLite + `FakeLLMProvider`）：

| Demo | 链路 | 断言落点 |
|---|---|---|
| Research Agent | Chat 内联 Workflow | `workflow.node.started/completed` 走完 `planner` / `writer`；`runs.kind=workflow` 且 `workflow_run_id` 非空 |
| Data Analysis Agent | Tool Calling | `tool.call.started/completed`；`tool_invocations` 行（`calculator` / `allow` / `succeeded`）；`run.tool_call_count=1` |
| Knowledge Assistant | RAG 引用 | `retrieval.completed`（`kb_ids` / `hit_count>=1`）；恰好 1 个 `retriever` span（`strategy=prefetch`、`chunk_ids` 非空） |

## 7. 评测结果

### 7.1 契约用例集（离线，CI 里跑的就是这条）

`configs/eval/contract-suite.yaml` 用 `FakeLLMProvider`，断言的是**平台机制**（工具真的被调用、失败被回填、
预检索不打断对话），不是模型能力。本机实测（`pytest tests/integration/test_eval_contract_suite.py`）：

| 指标 | 实测值 |
|---|---|
| 启用用例 | 5 条（`task-6-kb-search` 属迭代 E，`enabled: false` 不计入分母） |
| `task_success_rate` | **1.00**（5/5 passed） |
| `tool_success_rate` | **0.75**（4 行 `tool_invocations`，`task-4-tool-error-backfill` 那行是 `failed` / `TOOL_EXECUTION_FAILED`） |
| `workflow_success_rate` | `null`（该用例集没有 workflow 用例 —— 没有分母就报 `null`，不报 0） |
| `avg_tool_calls` | **0.8** |
| 预检索 | 每个 Run 恰好 1 个 `retriever` span（`strategy=prefetch`、`output.chunk_ids` 非空） |

复现：`cd backend; .\.venv\Scripts\pytest.exe -q tests/integration/test_eval_contract_suite.py`。

### 7.2 真实模型用例集（`demo-suite`）

`configs/eval/demo-suite.yaml` 跑在 `research-agent` 上（计算 / 预检索 + 引用 / 多步工具 / 表达质量），
含 `llm_judge` 参考项。**报告是本地产物**：`backend/data/` 不进版本库，所以本文不贴文件，只给复现方式。

```powershell
cd backend
# 先按 §2.2 配好 Provider，再：
.\.venv\Scripts\python.exe -m app.scripts.evaluate --suite demo-suite --agent research-agent
# 若 `--agent` 解析不到，换成 /agents 列表里的 Agent id
```

报告落 `backend/data/reports/eval-<时间戳>.md|json`（`REPORT_KEEP` 轮转）；把 Markdown 贴到本节即可。
两条口径（4.9.4）：**确定性断言优先**，`llm_judge` 只作参考（报告里同时给分数分布）；`demo-suite`
只记录**实测值**，不承诺 100%。

> **诚实说明**：本机（v1.21 收口时）没有配置真实模型 Key，因此 §7.1 的离线数字是实测的，
> 而 `demo-suite` 的真实模型路径**尚未在本机跑过** —— 这一点在《详细设计》附录 F 里同样如实标注。

## 8. 开发者指南

### 8.1 本地开发（`dev.ps1` / 迁移 / 测试 / lint）

```powershell
# 后端：虚拟环境 + 依赖 + 迁移 + 起服务
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 一键起前后端（两个窗口；会先 alembic upgrade head）
cd ..; .\scripts\dev.ps1

# 迁移 / 就绪探针（check_readyz 等价 GET /readyz，不需要起 HTTP 服务）
.\scripts\migrate.ps1                  # alembic upgrade head
.\scripts\migrate.ps1 downgrade base    # 回滚到空库
cd backend
.\.venv\Scripts\python.exe -m alembic check            # 迁移与 ORM 模型无差异
.\.venv\Scripts\python.exe -m app.scripts.check_readyz

# 后端质量门禁（与 CI 一致）
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe app
.\.venv\Scripts\pytest.exe -q

# 前端
cd ..\frontend
npm install
npm run lint          # eslint
npm run format:check  # prettier
npm run typecheck     # tsc --noEmit
npm run test          # vitest
```

改后端路由后刷新前端类型（CI 会校验"生成结果与提交版本一致"）：

```powershell
cd backend; .\.venv\Scripts\python.exe -m app.scripts.export_openapi
cd ..\frontend; npm run gen:api
```

### 8.2 加一个内置工具（5 步）

1. 在 `app/runtime/tools/builtin/` 实现工具（规格含名字 / 描述 / JSON Schema / 执行函数，走九步流水线）；
2. 在 `app/runtime/tools/registry.py` 注册（`builtin_tool_id()` 给确定性 id，启动时 `sync_builtin_tools()` 同步进库）；
3. 权限分级与默认开关写进规格（如 `python_execute` 受 `PYTHON_EXECUTE_ENABLED` 约束）；
4. 单测放 `tests/unit/`（参数校验 / 沙箱或权限拒绝 / 正常路径各一条）；
5. Agent 侧只在 YAML / UI 的 `tools` 里加名字 —— **不动 runtime**。

### 8.3 加一个 Provider / VectorStore / WorkflowEngine

实现对应 `base.py` 协议 → 在 `registry.py` 按 `kind` 注册 → 加进 `tests/unit/contract/` 的参数化列表
（契约测试是"换实现不改调用方"的保证）。注意 SD-10 / SD-11：**先有第二个真实调用方**再写第二套实现（R11 红线）。

### 8.4 加一个 API

分层规则：`api/*`（协议转换 + 参数校验）→ `services/*`（事务与跨实体编排）→ `db/models`；
`runtime/**` **不许** import `services` / `api`。新增路由后按 §8.1 重导 `docs/openapi.json` 与
`frontend/src/types/api.d.ts`，否则 CI 的 `gen:api` 门禁会红。

---

## 9. 已知限制与再评估触发条件

逐条对应《详细设计》0.4.2，以及"什么信号下值得重开"（10.3）：

- **`/stats` 看板只做数字**（`Statistic` + `Descriptions`）：7.9 的 MVP 口径把"Stats 图表美化"划给迭代 A–D，
  因此没有引入图表库（SD-14 红线①：不出现没有第二调用方的依赖）。
- **`kb_search` 工具不存在**（按需检索属迭代 E）：MVP 的知识库链路是 Agent 的固定预检索（4.4.2 模式 a）；
  `configs/eval/demo-suite.yaml` 的断言已按这个口径修正（不再期望 `kb_search` 出现在工具序列里）。
- **`demo-suite` 的 `llm_judge` 路径尚未对真实模型跑过**（7.8 遗留，v1.21 如实标注）；
  CI 里跑的是 `contract-suite`（`FakeLLMProvider`，无网络依赖）。
- 单进程 + SQLite，无多实例/横向扩展；不引入 Redis / Celery / PostgreSQL / 向量库集群（SD-8 / SD-9 / SD-11）。
- 无鉴权（`owner_key` 固定 `local`，SD-3）；请勿直接暴露到公网。
- Agent 可带工具、Workflow 与知识库：`tool_ids` 必须指向**存在且 `enabled`** 的工具；`workflow_id` 必须指向**已发布**的 Workflow；`knowledge_base_ids` 必须指向**未删除且可检索**的知识库（任一不满足 → `AGENT_INVALID_CONFIG`）。
- 工具执行是**顺序**的（SD-2，同一 step 内逐个调用）；同一工具连续失败 3 次才终止 Run（`TOOL_REPEATED_FAILURE`）。
- 同一会话同时只允许一个活跃 Run（第二个请求 409）；客户端断开**默认不取消** Run（`DETACH_CANCEL=true` 才取消，SD/3.4）。
- Prompt 历史只做"变更留档 + 只读列表"，**回滚端点**（`prompt-versions/{version}/activate`）属延后项（《详细设计》7.0.1）。
- Workflow 不支持并行分支（SD-1）；节点**串行**执行；`human` 节点与审批属 Backlog（SD-17）。
- Workflow 的 `retriever` 节点自 Phase 5 起接真实检索（4.5.2 复用 `kb_service.retrieve`）：`kb_id` 必须是**已存在的知识库 id**（示例图里的 `kb_id: product-docs` 是占位，写库时由 Phase 8 的 `seed_demo` 解析成真实 id）；在那之前跑示例图会在该节点得到 `KB_NOT_FOUND` —— 节点默认 `on_error=continue`，错误文本写进 `output_key` 后继续后续节点，图的形状不变。
- Workflow 运行**不返回 SSE**（3.4）：`POST /workflows/{id}/runs` 返回 202，页面按 2s 轮询 `node-runs`；SSE 只在 Chat 内联场景（`agent.workflow_id` 非空）里出现。
- `python_execute` / `file_write` 默认关闭，需在 `Settings` 显式开启（SD-17）。
- Memory / MCP / 审批后台 / 评测平台为 Backlog：设计保留在文档中，但**不提供接口、不建空页面**（SD-14②）。

---

## 10. 许可

MIT，见 [LICENSE](LICENSE)。
