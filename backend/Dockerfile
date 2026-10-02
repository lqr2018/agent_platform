# 后端镜像（详细设计 6.1：python:3.11-slim；启动 = 迁移 + uvicorn）
# 构建上下文是**仓库根**（见 docker/docker-compose.yml 的 context: ..）
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl 供 compose 的 healthcheck 使用（`curl -f http://localhost:8000/readyz`，6.1）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷业务代码，最大化利用层缓存
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/app ./app
RUN pip install --upgrade pip && pip install .

# 迁移脚本与配置（alembic.ini 必须为纯 ASCII：configparser 按系统本地编码读取）
COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic

# 运行时数据目录（SQLite / chroma / 沙箱 / 报告）；compose 把它挂成卷
RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -f http://localhost:8000/readyz || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
