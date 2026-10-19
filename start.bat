@echo off
REM 使用项目虚拟环境启动后端；先按 docs/environment.md 安装锁定依赖。
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [SmartBrief] Missing .venv. Follow docs/environment.md first.
  exit /b 1
)
echo [SmartBrief] Starting backend (http://localhost:8002) ...
start "SmartBrief-Backend" /min cmd /k "cd /d backend && ..\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8002"
echo [SmartBrief] Starting frontend (http://localhost:5174) ...
cd frontend
call npm run dev
