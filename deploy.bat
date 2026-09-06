@echo off
REM ASES Production Deployment Script for Windows
REM Requires: Docker Desktop, WSL2, Git

setlocal EnableDelayedExpansion

echo ============================================================
echo   ASES - Autonomous Software Engineering System
echo   Production Deployment for Windows
echo ============================================================
echo.

REM Check prerequisites
where docker >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Docker not found. Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/
    exit /b 1
)

where git >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Git not found. Install Git for Windows.
    exit /b 1
)

REM Check WSL2
docker info 2>nul | findstr "WSL2" >nul
if %errorlevel% neq 0 (
    echo [WARN] WSL2 backend recommended for better performance.
)

echo [INFO] Pre-flight checks passed.
echo.

REM Step 1: Environment
echo Step 1/5: Checking environment...
if not exist ".env" (
    echo [INFO] Creating .env from template...
    copy .env.example .env >nul
    echo [WARN] .env created with defaults. EDIT IT NOW with your API keys.
    echo [WARN] Required: OPENAI_API_KEY, GITHUB_TOKEN
    notepad .env
)

REM Step 2: SSL Certificates
echo.
echo Step 2/5: SSL Certificates...
if not exist "ssl" mkdir ssl
if not exist "ssl\fullchain.pem" (
    echo [WARN] SSL certificates not found in ssl/ folder.
    echo [INFO] For local testing, generate self-signed:
    echo   openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout ssl/privkey.pem -out ssl/fullchain.pem
    echo.
    echo [INFO] For production, use Let's Encrypt or place your certs in ssl/
    pause
)

REM Step 3: Build
echo.
echo Step 3/5: Building images...
call build.bat

REM Step 4: Start
echo.
echo Step 4/5: Starting services...
docker compose up -d

echo.
echo Waiting for services to initialize...
timeout /t 20 /nobreak >nul

REM Step 5: Verify
echo.
echo Step 5/5: Verification...
echo.
echo === Service Status ===
docker compose ps
echo.

REM Health checks
curl -s http://localhost:8000/health | findstr "healthy" >nul && (
    echo [OK] Agent service is healthy
) || (
    echo [WARN] Agent service not responding yet. Check: docker compose logs agent
)

curl -s http://localhost:5678/healthz | findstr "200\|401" >nul && (
    echo [OK] n8n is responding
) || (
    echo [WARN] n8n not responding yet. Check: docker compose logs n8n
)

echo.
echo ============================================================
echo   DEPLOYMENT COMPLETE
echo ============================================================
echo.
echo URLs:
echo   n8n UI:       http://localhost
echo   Agent API:    http://localhost/api/
echo   API Docs:     http://localhost/api/docs
echo.
echo Next Steps:
echo   1. Visit http://localhost and set up n8n admin account
echo   2. Settings ^> Credentials ^> Add PostgreSQL, Telegram, OpenAI
echo   3. Workflows ^> Import from File ^> n8n_orchestrator.json
echo   4. Activate the workflow
echo.
echo Useful Commands:
echo   run.bat logs        - View all logs
echo   run.bat logs agent  - View agent logs
echo   run.bat shell       - Open agent shell
echo   run.bat db          - Open database shell
echo   run.bat backup      - Manual backup
echo   run.bat test        - Test agent
echo.
pause
