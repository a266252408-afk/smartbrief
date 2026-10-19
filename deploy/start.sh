#!/usr/bin/env bash
# =====================================================================
# SmartBrief container deployment one-click launcher (Linux / Mac)
# 1. copy .env.example -> .env if missing (fill real API keys first)
# 2. build images
# 3. start services in background
# Frontend: http://localhost:8080  |  Backend API: http://localhost:8002
# =====================================================================
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "[deploy] .env not found, copied from .env.example."
  echo "         Please edit deploy/.env to fill OPENAI_API_KEY / EMBEDDING_API_KEY, then run again."
  cp .env.example .env
  exit 1
fi

echo "[deploy] Building images ..."
docker compose build

echo "[deploy] Starting services in background ..."
docker compose up -d

echo "[deploy] Done."
echo "  Frontend:  http://localhost:8080"
echo "  Backend:   http://localhost:8002/api/v1/health"
docker compose ps