# 前端镜像：多阶段构建（详细设计 6.1：node:20 构建 → nginx:alpine 托管静态资源）
# 构建上下文是**仓库根**（见 docker/docker-compose.yml 的 context: ..）
FROM node:20-alpine AS build

WORKDIR /app

# 依赖层缓存：先拷 lockfile
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# src/types/api.d.ts 是提交进仓库的生成产物（3.5），构建时直接使用，无需后端在线
RUN npm run build

FROM nginx:alpine AS runtime

# SSE 关键配置写在 docker/nginx.conf（proxy_buffering off; proxy_read_timeout 300s）
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
