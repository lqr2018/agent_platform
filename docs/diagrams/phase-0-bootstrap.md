# Phase 0 启动与请求链路

对应《详细设计》7.1（Phase 0）与 6.3（启动自检与健康检查）。**Phase 0 无业务表，只有 `alembic_version`。**

## 1. 进程启动（lifespan）

```text
uvicorn app.main:app
  └─ create_app()
       ├─ configure_logging(Settings)          # dev 彩色 / test+prod JSON（1.5.1）
       ├─ add_middleware(RequestIdMiddleware)  # 纯 ASGI，SSE 安全（7.1 任务 1）
       ├─ add_middleware(CORSMiddleware)       # CORS_ORIGINS
       ├─ register_exception_handlers()        # 1.6：AppError / 422 / IntegrityError / 405 / 未捕获
       ├─ include_router(probe_router)         # /healthz、/readyz（根路径，3.1）
       └─ include_router(api_router, "/api/v1")
     │
  lifespan 进入（6.3）
     1. Settings.assert_production_secrets()   # prod 下占位密钥 → CONFIG_INVALID，直接失败（6.2）
     2. 创建 DATA_DIR 下的目录：data/ chroma/ files/ uploads/ reports/
     3. verify_migrations_at_head()            # 数据库版本 ≠ head → 抛错退出（防止 schema 漂移）
     4. app.state.features = build_features()  # /api/v1/meta 的能力开关快照
  ⋮  服务中
  lifespan 退出 → dispose_engine()
```

> 6.3 的第 2、3 条（内置工具与 DB 对齐、孤儿 Run 收敛）需要 `tools` / `runs` 表，分别在 **Phase 2 / Phase 3** 接入，Phase 0 不实现。

## 2. 一次请求（含异常路径）

```text
client
  │  GET /api/v1/meta        （或 /healthz、/readyz、任意错误请求）
  ▼
RequestIdMiddleware（纯 ASGI）
  ├─ 复用入站 X-Request-Id，缺失则生成 32 位十六进制（0.2.1）
  ├─ 写入 ContextVar → 之后每条日志自动带 request_id（1.5.1）
  ├─ scope["state"]["request_id"] 兜底（500 由 ServerErrorMiddleware 在中间件之外处理）
  ▼
路由匹配
  ├─ 命中 → 端点函数（只做协议转换，1.2）→ {data, meta}
  └─ 未命中 / 方法不符 → 405 / 404 → 异常处理器 → {error, meta}
  ▼
异常处理器（1.6，统一信封）
  ├─ AppError        → 按其 code / http_status
  ├─ RequestValidationError → VALIDATION_ERROR 422 + details.errors[]
  ├─ IntegrityError  → UNIQUE → CONFLICT 409；FOREIGN KEY → 422
  ├─ HTTPException   → 404 / 405 / …
  └─ 其它 Exception  → INTERNAL_ERROR 500（堆栈只进日志，响应体不含）
  ▼
RequestIdMiddleware 收尾
  ├─ 响应头回写 X-Request-Id
  └─ 记录 http.request（method / path / status / duration_ms / request_id）
```

## 3. `/readyz` 的检查项（Phase 0）

```text
GET /readyz
  ├─ database     : SELECT 1 → BEGIN IMMEDIATE + ROLLBACK（可写）→ alembic_version == head
  └─ vector_store : VECTOR_STORE_KIND=chroma → 目录存在且可写
                    VECTOR_STORE_KIND=memory → 进程内实现（恒 ok）
  全部通过 → 200 {"status":"ok"}；任一失败 → 503 {"status":"degraded"} + 具体原因
```

> Phase 5 接入 Chroma 后，"目录可写"会被替换为真实的 collection 探测；届时本条图示同步更新。
