@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM ASES Quick Start — Windows
REM One-command setup for local development
REM Requires: Docker Desktop (WSL2 backend recommended)
REM ═══════════════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion

echo.
echo ╔══════════════════════════════════════════════════════════════════════╗
echo ║         ASES — Autonomous Software Engineering System                ║
echo ║              Quick Start (Windows)                                   ║
echo ╚══════════════════════════════════════════════════════════════════════╝
echo.

REM ── Check Docker ──────────────────────────────────────────────────────────
echo [CHECK] Verifying Docker installation...
where docker >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Docker not found.
    echo.
    echo Please install Docker Desktop for Windows:
    echo   https://docs.docker.com/desktop/install/windows-install/
    echo.
    echo After installation:
    echo   1. Start Docker Desktop
    echo   2. Enable WSL2 backend ^(Settings ^> General ^> Use WSL2^)
    echo   3. Re-run this script
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%a in ('docker --version') do set "DOCKER_VER=%%a"
echo [OK] Docker found: %DOCKER_VER%

REM Check Docker is running
docker info >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Docker Desktop is not running.
    echo Please start Docker Desktop and wait for it to initialize.
    echo.
    pause
    exit /b 1
)
echo [OK] Docker Desktop is running

REM Check Docker Compose
docker compose version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Docker Compose not available.
    exit /b 1
)
echo [OK] Docker Compose available

REM ── Check WSL2 ──────────────────────────────────────────────────────────
docker info 2>nul | findstr /i "wsl2" >nul
if %errorlevel% equ 0 (
    echo [OK] WSL2 backend detected ^(optimal performance^)
) else (
    echo [WARN] WSL2 backend not detected. Linux containers may run slower.
    echo         Enable in Docker Desktop: Settings ^> General ^> Use WSL2
)

REM ── Environment Setup ───────────────────────────────────────────────────
echo.
echo [SETUP] Checking environment configuration...

if not exist ".env" (
    echo.
    echo [INFO] Creating .env from template...
    copy /y .env.example .env >nul

    echo.
    echo ╔══════════════════════════════════════════════════════════════════════╗
    echo ║  ACTION REQUIRED: Configure .env file                                ║
    echo ╚══════════════════════════════════════════════════════════════════════╝
    echo.
    echo The .env file has been created with default values.
    echo You MUST edit it and add your API keys before continuing.
    echo.
    echo REQUIRED keys to add:
    echo   1. OPENAI_API_KEY     — Get from https://platform.openai.com/api-keys
    echo   2. GITHUB_TOKEN       — Get from https://github.com/settings/tokens
    echo.
    echo OPTIONAL keys:
    echo   3. VERCEL_TOKEN       — For deployments ^(optional^)
    echo   4. TELEGRAM_BOT_TOKEN — For notifications ^(optional^)
    echo   5. UPWORK_RSS_URL     — For lead discovery ^(optional^)
    echo.
    echo The .env file will now open in Notepad.
    echo Save and close Notepad when done.
    echo.
    pause

    start /wait notepad .env

    echo.
    echo [INFO] Checking if .env was configured...
    findstr /c:"sk-" .env >nul 2>nul || findstr /c:"ghp_" .env >nul 2>nul
    if %errorlevel% neq 0 (
        echo [WARN] API keys not detected in .env.
        echo [WARN] Services may fail to start without valid keys.
        echo.
        choice /c YN /m "Continue anyway"
        if !errorlevel! equ 2 exit /b 1
    )
) else (
    echo [OK] .env file exists
)

REM ── SSL Certificates ────────────────────────────────────────────────────
if not exist "ssl" mkdir ssl >nul 2>nul

if not exist "ssl\fullchain.pem" (
    echo.
    echo [INFO] SSL certificates not found. Generating self-signed for local testing...

    REM Check if OpenSSL is available
    where openssl >nul 2>nul
    if %errorlevel% equ 0 (
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 ^
            -keyout ssl\privkey.pem ^
            -out ssl\fullchain.pem ^
            -subj "/C=US/ST=Local/L=Local/O=ASES/CN=localhost" ^
            2>nul
        echo [OK] Self-signed certificates generated in ssl/
    ) else (
        echo [WARN] OpenSSL not found. Please install OpenSSL or provide your own certificates.
        echo         Place fullchain.pem and privkey.pem in the ssl/ directory.
        pause
    )
)

REM ── Start Services ──────────────────────────────────────────────────────
echo.
echo [START] Starting ASES services...
echo         This may take 2-3 minutes on first run ^(images will be downloaded^).
echo.

docker compose up -d

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to start services.
    echo [INFO] Check: docker compose logs
    pause
    exit /b 1
)

REM ── Wait for initialization ──────────────────────────────────────────────
echo.
echo [WAIT] Waiting for services to initialize ^(30 seconds^)...
timeout /t 30 /nobreak >nul

REM ── Health Checks ───────────────────────────────────────────────────────
echo.
echo [CHECK] Running health checks...
echo.

set "ALL_HEALTHY=1"

REM Check Agent
curl -s http://localhost:8000/health | findstr "healthy" >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Agent Service    : http://localhost:8000/health  ^✓
) else (
    echo   [WARN] Agent Service  : http://localhost:8000/health  ^✗ ^(still starting?^)
    set "ALL_HEALTHY=0"
)

REM Check n8n
curl -s -o nul -w "%%{http_code}" http://localhost:5678/healthz | findstr "200 401" >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] n8n Orchestrator : http://localhost:5678/healthz ^✓
) else (
    echo   [WARN] n8n Orchestrator: http://localhost:5678/healthz ^✗ ^(still starting?^)
    set "ALL_HEALTHY=0"
)

REM Check Nginx
curl -s -o nul -w "%%{http_code}" http://localhost/ | findstr "200 301 302" >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Nginx Proxy      : http://localhost/            ^✓
) else (
    echo   [WARN] Nginx Proxy    : http://localhost/            ^✗
    set "ALL_HEALTHY=0"
)

REM Check PostgreSQL
docker compose exec -T postgres pg_isready -U ases >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] PostgreSQL       : localhost:5432                ^✓
) else (
    echo   [WARN] PostgreSQL     : localhost:5432                ^✗
    set "ALL_HEALTHY=0"
)

REM Check Redis
docker compose exec -T redis redis-cli ping | findstr "PONG" >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Redis            : localhost:6379                ^✓
) else (
    echo   [WARN] Redis          : localhost:6379                ^✗
    set "ALL_HEALTHY=0"
)

REM ── Summary ──────────────────────────────────────────────────────────────
echo.
echo ╔══════════════════════════════════════════════════════════════════════╗

if "%ALL_HEALTHY%"=="1" (
    echo ║  ✓ ALL SERVICES HEALTHY                                              ║
) else (
    echo ║  ⚠ SOME SERVICES STILL STARTING                                      ║
)

echo ╚══════════════════════════════════════════════════════════════════════╝
echo.
echo Access Points:
echo   ┌────────────────────────────────────────────────────────────────────┐
echo   │  n8n Web UI       : http://localhost                               │
echo   │  Agent API        : http://localhost/api/                          │
echo   │  API Documentation: http://localhost/api/docs                      │
echo   │  PostgreSQL       : localhost:5432  (user: ases)                   │
echo   │  Redis            : localhost:6379                                 │
echo   └────────────────────────────────────────────────────────────────────┘
echo.

if "%ALL_HEALTHY%"=="0" (
    echo [INFO] Some services are still initializing. Wait 30 seconds and check again:
    echo         docker compose ps
    echo         docker compose logs [service_name]
    echo.
)

echo Next Steps:
echo   1. Visit http://localhost in your browser
echo   2. Set up n8n admin account (first visit)
echo   3. Settings ^> Credentials ^> Add:
echo       • PostgreSQL (host: postgres, db: ases_production)
echo       • Telegram API (token from @BotFather)
echo       • OpenAI API
echo   4. Workflows ^> Import from File ^> n8n_orchestrator.json
echo   5. Activate the workflow
echo.
echo Useful Commands:
echo   run.bat logs        - View all logs
echo   run.bat logs agent  - View agent logs
echo   run.bat shell       - Open agent shell
echo   run.bat test        - Test agent service
echo   run.bat backup      - Manual backup
echo   run.bat costs       - Show execution costs
echo.
echo To stop:
echo   run.bat down
echo.
pause