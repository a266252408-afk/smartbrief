# =====================================================================
# SmartBrief 前端镜像（多阶段构建：node 构建 → nginx 运行）
# 构建上下文 = 仓库根目录（smartbrief/），由 docker-compose.yml 指定
#   阶段一：npm ci + npm run build（vue-tsc 类型检查 + vite 打包 dist）
#   阶段二：nginx:alpine 只保留 dist 静态产物与反代配置，镜像小。
# =====================================================================

# ---------- 阶段一：构建 ----------
FROM node:20-alpine AS build
WORKDIR /app

# 先只拷贝依赖清单（package-lock.json 保证可复现），命中层缓存时跳过 npm ci
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# 再拷贝源码并构建（.dockerignore 已排除 node_modules/dist）
COPY frontend/ ./
RUN npm run build

# ---------- 阶段二：运行（nginx）----------
FROM nginx:1.27-alpine

# 反代配置：/api 转发到后端容器（backend = compose 服务名），SPA 路由回退
COPY deploy/frontend/nginx.conf /etc/nginx/conf.d/default.conf
# 静态产物：Vue 构建输出
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]