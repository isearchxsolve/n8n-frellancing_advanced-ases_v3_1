@echo off
REM ASES Docker Build Script for Windows

set "IMAGE_NAME=ases-agent"
set "TAG=latest"

if not "%~1"=="" set "TAG=%~1"

echo ============================================================
echo   ASES Docker Build
echo   Image: %IMAGE_NAME%:%TAG%
echo ============================================================

REM Check Docker
where docker >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Docker not found. Install Docker Desktop first.
    exit /b 1
)

echo.
echo Building agent service...
docker build -t %IMAGE_NAME%:%TAG% -t %IMAGE_NAME%:%date:~-4,4%%date:~-10,2%%date:~-7,2% --build-arg BUILDKIT_INLINE_CACHE=1 ./agent_service

echo.
echo [SUCCESS] Build complete: %IMAGE_NAME%:%TAG%
echo.
echo Run:
echo   docker compose up -d
goto :eof
