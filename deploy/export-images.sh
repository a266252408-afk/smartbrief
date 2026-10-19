#!/usr/bin/env bash
# =====================================================================
# SmartBrief: package local images into a single transferable tarball
# Output: deploy/smartbrief-images.tar.gz (load on target with:
#         docker load -i smartbrief-images.tar.gz)
# Usage: ./export-images.sh   (local machine, after docker compose build)
# =====================================================================
set -e
cd "$(dirname "$0")"

echo "[export] Building images (skip if already up-to-date) ..."
docker compose build

echo "[export] Saving smartbrief-backend:1.0 + smartbrief-frontend:1.0 ..."
docker save smartbrief-backend:1.0 smartbrief-frontend:1.0 | gzip > smartbrief-images.tar.gz

echo "[export] Done:"
ls -lh smartbrief-images.tar.gz