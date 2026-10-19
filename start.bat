@echo off
REM =====================================================================
REM SmartBrief one-click launcher (Windows)
REM - Backend FastAPI (port 8002): starts in a separate window.
REM   The built-in MCP Server is auto-started by the backend.
REM - Frontend Vite (port 5174): starts in the foreground, Ctrl+C to stop.
REM Prerequisites:
REM   - configure backend/.env (OPENAI_* / EMBEDDING_*)
REM   - pip install -r backend/requirements.txt
REM   - cd frontend && npm install
REM =====================================================================
cd /d "%~dp0"

echo [SmartBrief] Starting backend (http://localhost:8002) ...
start "SmartBrief-Backend" cmd /k "cd /d backend && uvicorn main:app --host 0.0.0.0 --port 8002"

echo [SmartBrief] Starting frontend (http://localhost:5174) ...
cd frontend
call npm run dev