# Agent Platform —— 配置驱动的 LLM Agent 编排与执行平台

一个「配置驱动」的 Agent 平台：用配置定义 Agent（模型 / Prompt / 工具 / 知识库 / 工作流），
平台负责**执行、编排、可观测**，把每一次运行的全链路（Run → Agent → LLM / Tool / Retriever）落成可查询的 Trace。

文档是唯一事实来源：

| 文档 | 作用 |
|---|---|
| [`docs/设计大纲.md`](docs/设计大纲.md) | 需求与总体架构（Why / What） |
| [`docs/详细设计.md`](docs/详细设计.md) | 实现契约：目录、数据模型、API、运行时、测试、实施计划（How） |

---

## 1. 这是什么 / 不是什么

**是**：一个可本地一键起、能当场演示「配置一个 Agent → 带工具与知识库跑起来 → 看到完整 Trace」的平台；
每个模块都有清晰接口、可替换实现（Provider 抽象）与测试。

**不是**（第一阶段明确不做，见《详细设计》0.4.2 Non-Goals）：不做多租户与鉴权、不做并行 Workflow 分支、
不做人工审批后台、不做 MCP、不做长期记忆、不做评测平台 UI、不做分布式部署（单进程 + SQLite）。

---

## 2. 30 秒快速开始

> 详细步骤见《详细设计》6.1。当前进度见第 5 节。

```powershell
# 1) 后端：虚拟环境 + 依赖 + 迁移 + 起服务
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000

# 2) 前端：装依赖 + 起 dev server（5173，自动代理 /api、/healthz、/readyz）
cd ..\frontend
npm install
npm run dev

# 3) 验证
curl http://localhost:8000/healthz   # {"status":"ok",...}
curl http://localhost:8000/readyz    # {"status":"ok","checks":[...]}
curl http://localhost:8000/api/v1/meta
```

容器方式（`docker/docker-compose.yml`，api + frontend 两个服务）：

```bash
docker compose -f docker/docker-compose.yml up --build   # 打开 http://localhost:8080
```

---

## 3. 架构

```text
api  →  services  →  db/models
                 ↘  runtime/*  →  runtime/observability
frontend  →  /api/v1 (HTTP + SSE)  →  api
```

- **api**：协议转换、参数校验、组装 DTO；
- **services**：事务边界、实体 CRUD、跨实体编排；
- **runtime**：纯执行逻辑，**不 import services/api**，通过入参 dataclass 拿到数据 → 可单测、可脱离 DB 运行；
- 每个可替换家族（LLM / Embedding / VectorStore / Memory / WorkflowEngine / Tool）都是 `base.py` 抽象 + `registry.py` 按 `kind` 实例化。

---

## 4. 平台能力（按阶段交付）

| 能力 | 状态 |
|---|---|
| 基础设施：配置 / 日志 / 错误模型 / 迁移 / 健康检查 | ✅ Phase 0 |
| Agent Runtime：Provider / Agent CRUD / 流式对话 / Trace | ✅ Phase 1（M1） |
| Tool Calling：Tool Registry / 权限分级 / 沙箱 | ✅ Phase 2（M2） |
| Workflow：串行图 / 状态落库 / 断点续跑 | ✅ Phase 3（M2） |
| RAG：知识库 / 摄取 / 检索 / 引用 | Phase 5（M3） |
| Trace 与轻量评测（`scripts/evaluate.py`） | Phase 7（M3） |
| Memory / MCP / 审批后台 / 评测平台 | 设计已就位，**不在第一阶段**（《详细设计》0.5.3 Backlog） |

---

## 5. 当前进度

**已完成：Phase 0 项目基础设施 + Phase 1 最小 Agent Runtime + Phase 2 Tool Calling + Phase 3 Workflow**（对齐《详细设计》7.1 / 7.2 / 7.3 / 7.4）

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

**验证情况（Phase 0–3）**：后端 `pytest` **501 项**（498 passed + 3 skipped）、覆盖率 **91%**（`TOTAL 6858 621 91%`）、`ruff check` / `ruff format --check` 0 告警、`mypy app` 通过、`alembic` 往返 + `alembic check`（`No new upgrade operations detected`）、就绪探针 `python -m app.scripts.check_readyz` 通过（`ok=True revision 0004_phase3_workflow_tables`）、`export_openapi` 幂等（连续两次同哈希）；前端 `eslint` / `prettier` / `tsc` / `vitest`（**28 项**）/ `vite build` 全绿。Phase 1/2 的回归用例全绿（无 Workflow 的 Agent 与 Chat 行为不变）；Phase 3 的端到端链路（真实 `create_app()` + lifespan + SQLite：建图 → 发布 → 202 运行 → 轮询 `node-runs` → resume/cancel/内联 Chat 流）见 `tests/integration/test_workflow_runner.py`。说明：`app/services/**` 与 `app/api/**` 的行覆盖率在本机 coverage 下会漏记"`await` 之后的尾行"（《详细设计》7.2 记录的口径），因此服务层另有直测补充（`tests/integration/test_workflow_runner.py` 直接断言落库结果与 `node_runs` 序列）。

下一步：**Phase 5 RAG / 知识库**（见《详细设计》7.6，M3）。

---

## 6. 本地开发

```powershell
# 一键起前后端（两个窗口）
.\scripts\dev.ps1

# 迁移
.\scripts\migrate.ps1            # alembic upgrade head
.\scripts\migrate.ps1 downgrade base

# 后端质量门禁（与 CI 一致）
cd backend
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe app
.\.venv\Scripts\pytest.exe -q

# 迁移与就绪探针（check_readyz 等价 GET /readyz，不需要起 HTTP 服务）
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check          # 迁移与 ORM 模型无差异
.\.venv\Scripts\python.exe -m app.scripts.check_readyz

# 前端
cd frontend
npm run lint        # eslint
npm run typecheck   # tsc --noEmit
npm run test        # vitest
npm run gen:api     # 由 ../docs/openapi.json 生成 src/types/api.d.ts（禁止手改）
```

改后端路由后刷新前端类型：

```powershell
cd backend; .\.venv\Scripts\python.exe -m app.scripts.export_openapi
cd ..\frontend; npm run gen:api
```

---

## 7. 已知限制

逐条对应《详细设计》0.4.2 与 10.3：

- 单进程 + SQLite，无多实例/横向扩展；不引入 Redis / Celery / PostgreSQL / 向量库集群（SD-8 / SD-9 / SD-11）。
- 无鉴权（`owner_key` 固定 `local`，SD-3）；请勿直接暴露到公网。
- Agent 可带工具与 Workflow：`tool_ids` 必须指向**存在且 `enabled`** 的工具；`workflow_id` 必须指向**已发布**的 Workflow（否则 `AGENT_INVALID_CONFIG`）；`knowledge_base_ids` 仍必须为空（Phase 5，SD-14②）。
- 工具执行是**顺序**的（SD-2，同一 step 内逐个调用）；同一工具连续失败 3 次才终止 Run（`TOOL_REPEATED_FAILURE`）。
- 同一会话同时只允许一个活跃 Run（第二个请求 409）；客户端断开**默认不取消** Run（`DETACH_CANCEL=true` 才取消，SD/3.4）。
- Prompt 历史只做"变更留档 + 只读列表"，**回滚端点**（`prompt-versions/{version}/activate`）属延后项（《详细设计》7.0.1）。
- Workflow 不支持并行分支（SD-1）；节点**串行**执行；`human` 节点与审批属 Backlog（SD-17）。
- Phase 3 的 Workflow 里 `retriever` 节点会明确回 `NOT_IMPLEMENTED`（知识库属 Phase 5）：节点默认 `on_error=continue`，错误文本会写进它的 `output_key` 并继续后续节点；`configs/workflows/*.yaml` 的示例图（`research-flow.yaml` / `kb-qa-flow.yaml`）因此在 Phase 5 之前只能跑到"检索失败"分支。
- Workflow 运行**不返回 SSE**（3.4）：`POST /workflows/{id}/runs` 返回 202，页面按 2s 轮询 `node-runs`；SSE 只在 Chat 内联场景（`agent.workflow_id` 非空）里出现。
- `python_execute` / `file_write` 默认关闭，需在 `Settings` 显式开启（SD-17）。
- Memory / MCP / 审批后台 / 评测平台为 Backlog：设计保留在文档中，但**不提供接口、不建空页面**（SD-14②）。

---

## 8. 许可

MIT，见 [LICENSE](LICENSE)。
