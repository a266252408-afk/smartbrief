#!/usr/bin/env bash
# =====================================================================
# SmartBrief: build locally -> package images -> upload -> deploy on
# a cloud/server host (Docker required on the target).
#
# Usage:
#   ./deploy-to-server.sh <user@host> [remote_dir]
#     user@host : SSH target, e.g. root@1.2.3.4 or ubuntu@my.server
#     remote_dir: deploy path on server (default /opt/smartbrief)
#
# What it does:
#   1. docker compose build (local)
#   2. docker save | gzip -> smartbrief-images.tar.gz
#   3. scp tarball + docker-compose.prod.yml + deployment env to server
#      (if deploy/.env exists locally it is uploaded too; otherwise
#       the server admin must create deploy/.env from .env.example)
#   4. ssh: docker load -> docker compose -f docker-compose.prod.yml up -d
#
# Requirements:
#   local & server: Docker Engine; local: OpenSSH scp/ssh client
#   server security group must allow ports 8080 (frontend) and 8002 (API)
# =====================================================================
set -e
cd "$(dirname "$0")"

SERVER="${1:?Usage: ./deploy-to-server.sh <user@host> [remote_dir]}"
REMOTE_DIR="${2:-/opt/smartbrief}"

echo "========== 1/4 Build local images =========="
docker compose build

echo "========== 2/4 Package images (docker save -> tar.gz) =========="
docker save smartbrief-backend:1.0 smartbrief-frontend:1.0 | gzip > smartbrief-images.tar.gz
ls -lh smartbrief-images.tar.gz

echo "========== 3/4 Upload to ${SERVER} (scp over SSH) =========="
ssh "$SERVER" "mkdir -p ${REMOTE_DIR}"
scp smartbrief-images.tar.gz docker-compose.prod.yml "$SERVER:/tmp/"
scp .env.example "$SERVER:${REMOTE_DIR}/.env.example"
# 本地已配置 .env 则一并上传（密钥经 SSH 加密通道传输，请确认服务器可信）
if [ -f .env ]; then
  echo "[deploy] uploading local deploy/.env (contains API keys!)"
  scp .env "$SERVER:${REMOTE_DIR}/.env"
fi

echo "========== 4/4 Load images & start on server =========="
ssh "$SERVER" "
  set -e
  docker load -i /tmp/smartbrief-images.tar.gz
  mv -f /tmp/docker-compose.prod.yml ${REMOTE_DIR}/docker-compose.prod.yml
  rm -f /tmp/smartbrief-images.tar.gz
  cd ${REMOTE_DIR}
  if [ ! -f .env ]; then
    cp .env.example .env
    echo '[deploy] .env created on server from .env.example — please edit it with real keys, then re-run:'
    echo \"        docker compose -f docker-compose.prod.yml up -d\"
    exit 1
  fi
  docker compose -f docker-compose.prod.yml up -d
  docker compose -f docker-compose.prod.yml ps
"

echo "========== Done =========="
echo "  Frontend: http://${SERVER#*@}:8080"
echo "  Health:   http://${SERVER#*@}:8002/api/v1/health"