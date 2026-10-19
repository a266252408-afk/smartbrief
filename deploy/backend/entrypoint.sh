#!/bin/sh
# =====================================================================
# SmartBrief 后端容器入口
# 职责：把镜像内预置的 RAG seed（模板 / 术语表）复制进卷挂载的 data 目录，
#       然后启动 uvicorn。cp -n（no-clobber）保证幂等：已存在文件不覆盖，
#       与本地“seed 只补只插”的约定一致。
# =====================================================================
set -e

echo "[entrypoint] 确保数据目录与 RAG 预置 seed 就绪..."
mkdir -p /app/backend/data/uploads
cp -rn /app/seed/backend-data/. /app/backend/data/ 2>/dev/null || true

echo "[entrypoint] 启动 uvicorn（$@）..."
exec "$@"