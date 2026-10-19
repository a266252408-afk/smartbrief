@echo off
REM =====================================================================
REM SmartBrief: package local images into a single transferable tarball
REM Output: deploy\smartbrief-images.tar (load on target with:
REM         docker load -i smartbrief-images.tar)
REM Usage: run in deploy\ (local machine, after docker compose build)
REM =====================================================================
cd /d "%~dp0"

echo [export] Building images (skip if already up-to-date) ...
docker compose build
if errorlevel 1 exit /b 1

echo [export] Saving smartbrief-backend:1.0 + smartbrief-frontend:1.0 ...
docker save -o smartbrief-images.tar smartbrief-backend:1.0 smartbrief-frontend:1.0
if errorlevel 1 exit /b 1

echo [export] Done:
dir smartbrief-images.tar