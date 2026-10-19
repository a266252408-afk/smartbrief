@echo off
REM =====================================================================
REM SmartBrief container deployment one-click launcher (Windows)
REM 1. copy .env.example -> .env if missing (fill real API keys first)
REM 2. build images
REM 3. start services in background
REM Frontend: http://localhost:8080  |  Backend API: http://localhost:8002
REM =====================================================================
cd /d "%~dp0"

if not exist .env (
  echo [deploy] .env not found, copied from .env.example.
  echo           Please edit deploy\.env to fill OPENAI_API_KEY / EMBEDDING_API_KEY, then run again.
  copy .env.example .env >nul
  exit /b 1
)

echo [deploy] Building images ...
docker compose build

echo [deploy] Starting services in background ...
docker compose up -d

echo [deploy] Done.
echo   Frontend:  http://localhost:8080
echo   Backend:   http://localhost:8002/api/v1/health
docker compose ps