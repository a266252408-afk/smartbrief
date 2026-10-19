#!/usr/bin/env bash
# =====================================================================
# SmartBrief 一键启动脚本（Linux / Mac）
#  - 后端 FastAPI（端口 8002）：后台启动（MCP Server 自动拉起）
#  - 前端 Vite（端口 5174）：前台启动，Ctrl+C 可停止
# 提示：首次运行前请先完成 backend/.env 配置与依赖安装
#       python -m venv .venv
#       .venv/bin/python -m pip install --require-hashes -r backend/requirements-dev.lock
#       cd frontend && npm ci
# =====================================================================
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[SmartBrief] 缺少 .venv，请先按 docs/environment.md 安装依赖。" >&2
  exit 1
fi

echo "[SmartBrief] 启动后端（http://localhost:8002）..."
(cd backend && ../.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8002) &

# 前端（前台）退出后回收后台后端进程
trap 'kill %1 2>/dev/null || true' EXIT

echo "[SmartBrief] 启动前端（http://localhost:5174）..."
cd frontend
npm run dev
