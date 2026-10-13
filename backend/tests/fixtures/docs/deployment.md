# 部署与运行

## 本地开发（SQLite + 本地 Chroma）

```bash
cd backend
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

前端在 `frontend/` 下 `npm install && npm run dev`，默认代理到 `http://localhost:8000`。

## 容器（docker compose）

`docker compose up --build` 会拉起 `api` 与 `web` 两个服务；数据库文件挂在卷上，重启不丢数据。

## 健康检查

- `GET /healthz`：进程存活探针；
- `GET /readyz`：逐项检查 database / migrations / vector_store / llm_providers，返回 503 时不要接流量。

## 环境变量（节选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db` | 数据库连接串 |
| `PYTHON_EXECUTE_ENABLED` | `false` | 是否允许 `python_execute` 工具 |
| `FILE_WRITE_ENABLED` | `false` | 是否允许 `file_write` 工具 |
| `WEB_SEARCH_PROVIDER` | 空 | 网络搜索后端（空表示该工具不可用） |
