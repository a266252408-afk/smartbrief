@echo off
REM =====================================================================
REM SmartBrief: build locally -> package images -> upload -> deploy on
REM a cloud/server host (Docker required on the target).
REM
REM Usage:
REM   deploy-to-server.bat <user@host> [remote_dir]
REM     user@host : SSH target, e.g. root@1.2.3.4 or ubuntu@my.server
REM     remote_dir: deploy path on server (default /opt/smartbrief)
REM
REM Notes:
REM   - remote commands run in bash (Linux server)
REM   - if deploy\.env exists locally it is uploaded too (API keys);
REM     otherwise create .env on the server from .env.example first
REM =====================================================================
cd /d "%~dp0"

set "SERVER=%~1"
if "%SERVER%"=="" (
  echo Usage: deploy-to-server.bat ^<user@host^> [remote_dir]
  exit /b 1
)
set "REMOTE_DIR=%~2"
if "%REMOTE_DIR%"=="" set "REMOTE_DIR=/opt/smartbrief"

echo ========== 1/4 Build local images ==========
docker compose build
if errorlevel 1 exit /b 1

echo ========== 2/4 Package images (docker save) ==========
docker save -o smartbrief-images.tar smartbrief-backend:1.0 smartbrief-frontend:1.0
if errorlevel 1 exit /b 1
dir smartbrief-images.tar

echo ========== 3/4 Upload to %SERVER% (scp over SSH) ==========
ssh %SERVER% "mkdir -p %REMOTE_DIR%"
if errorlevel 1 exit /b 1
scp smartbrief-images.tar docker-compose.prod.yml %SERVER%:/tmp/
if errorlevel 1 exit /b 1
scp .env.example %SERVER%:%REMOTE_DIR%/.env.example
if errorlevel 1 exit /b 1
if exist .env (
  echo [deploy] uploading local deploy\.env (contains API keys!)
  scp .env %SERVER%:%REMOTE_DIR%/.env
  if errorlevel 1 exit /b 1
)

echo ========== 4/4 Load images & start on server ==========
ssh %SERVER% "set -e; docker load -i /tmp/smartbrief-images.tar; mv -f /tmp/docker-compose.prod.yml %REMOTE_DIR%/docker-compose.prod.yml; cd %REMOTE_DIR%; if [ ! -f .env ]; then cp .env.example .env; echo '[deploy] .env created from .env.example - edit it with real keys then re-run: docker compose -f docker-compose.prod.yml up -d'; exit 1; fi; docker compose -f docker-compose.prod.yml up -d; docker compose -f docker-compose.prod.yml ps"
if errorlevel 1 exit /b 1

echo ========== Done ==========
echo   Frontend: http://%SERVER:*@=%
echo   Health:   http://%SERVER:*@=%