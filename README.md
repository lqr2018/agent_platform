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
| Tool Calling：Tool Registry / 权限分级 / 沙箱 | Phase 2（M2） |
| Workflow：串行图 / 状态落库 / 断点续跑 | Phase 3（M2） |
| RAG：知识库 / 摄取 / 检索 / 引用 | Phase 5（M3） |
| Trace 与轻量评测（`scripts/evaluate.py`） | Phase 7（M3） |
| Memory / MCP / 审批后台 / 评测平台 | 设计已就位，**不在第一阶段**（《详细设计》0.5.3 Backlog） |

---

## 5. 当前进度

**已完成：Phase 0 项目基础设施 + Phase 1 最小 Agent Runtime**（对齐《详细设计》7.1 / 7.2）

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

**验证情况**：后端 `pytest` 230 项（227 passed + 3 skipped）、覆盖率 92%（`runtime` 81–100%）、`ruff` / `mypy --strict` 0 告警、`alembic` 往返 + `alembic check` 通过；前端 `eslint` / `prettier` / `tsc` / `vitest`（20 项）/ `vite build` 全绿；并在**真实进程**里跑通「SSE 对话 → Run → Trace（3 span）」链路。

下一步：**Phase 2 Tool Calling**（见《详细设计》7.3，M2）。

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
- Phase 1 的 Agent 是**纯对话型**：`tool_ids` / `knowledge_base_ids` / `workflow_id` 必须为空，填了会返回 `AGENT_INVALID_CONFIG`（未实现的能力不提供入口，SD-14②）。
- 同一会话同时只允许一个活跃 Run（第二个请求 409）；客户端断开**默认不取消** Run（`DETACH_CANCEL=true` 才取消，SD/3.4）。
- Prompt 历史只做"变更留档 + 只读列表"，**回滚端点**（`prompt-versions/{version}/activate`）属延后项（《详细设计》7.0.1）。
- Workflow 不支持并行分支（SD-1）；工具调用顺序执行（SD-2）。
- `python_execute` / `file_write` 默认关闭，需在 `Settings` 显式开启（SD-17）。
- Memory / MCP / 审批后台 / 评测平台为 Backlog：设计保留在文档中，但**不提供接口、不建空页面**（SD-14②）。

---

## 8. 许可

MIT，见 [LICENSE](LICENSE)。
