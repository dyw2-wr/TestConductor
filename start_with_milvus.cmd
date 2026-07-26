@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1

docker version >nul 2>&1
if errorlevel 1 (
    echo [TestConductor] Docker Desktop is not available.
    echo [TestConductor] Start Docker Desktop, or use start.cmd without Milvus.
    exit /b 1
)

echo [TestConductor] Starting local Milvus dependencies...
docker compose -f infra\milvus\docker-compose.yml up -d --wait
if errorlevel 1 (
    echo [TestConductor] Milvus failed to become healthy.
    exit /b 1
)

echo [TestConductor] Milvus is healthy. Starting the web application...
call start.cmd %*
exit /b %errorlevel%

