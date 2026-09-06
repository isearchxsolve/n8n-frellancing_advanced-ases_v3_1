@echo off
REM ASES Multi-Container Run Script for Windows
REM Requires: Docker Desktop with WSL2 backend

setlocal EnableDelayedExpansion

set "CMD=%~1"
set "COMPOSE_FILE=docker-compose.yml"

if "%CMD%"=="" set "CMD=help"

goto :%CMD% 2>nul || goto :help

:up
:start
echo Starting ASES production stack...
docker compose -f "%COMPOSE_FILE%" up -d
echo.
echo Waiting for services...
timeout /t 15 /nobreak >nul
echo.
echo === Service Status ===
docker compose -f "%COMPOSE_FILE%" ps
echo.
echo URLs:
echo   n8n UI:       http://localhost
echo   Agent API:    http://localhost/api/
echo   API Docs:     http://localhost/api/docs
goto :eof

:down
:stop
echo Stopping ASES...
docker compose -f "%COMPOSE_FILE%" down
echo Stopped
goto :eof

:restart
if "%~2"=="" (
    echo Restarting all services...
    docker compose -f "%COMPOSE_FILE%" restart
) else (
    echo Restarting %~2...
    docker compose -f "%COMPOSE_FILE%" restart %~2
)
goto :eof

:logs
if "%~2"=="" (
    docker compose -f "%COMPOSE_FILE%" logs -f
) else (
    docker compose -f "%COMPOSE_FILE%" logs -f %~2
)
goto :eof

:ps
:status
docker compose -f "%COMPOSE_FILE%" ps
goto :eof

:build
echo Building images...
docker compose -f "%COMPOSE_FILE%" build --no-cache
goto :eof

:pull
echo Pulling latest images...
docker compose -f "%COMPOSE_FILE%" pull
goto :eof

:update
echo Updating ASES...
docker compose -f "%COMPOSE_FILE%" pull
docker compose -f "%COMPOSE_FILE%" up -d
goto :eof

:shell
if "%~2"=="" (
    docker compose -f "%COMPOSE_FILE%" exec agent bash
) else (
    docker compose -f "%COMPOSE_FILE%" exec %~2 bash
)
goto :eof

:db
docker compose -f "%COMPOSE_FILE%" exec postgres psql -U ases -d ases_production
goto :eof

:backup
set "BACKUP_DIR=%USERPROFILE%ses-backups"
set "DATE=%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "DATE=!DATE: =0!"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

echo Backing up database...
docker compose -f "%COMPOSE_FILE%" exec -T postgres pg_dump -U ases ases_production > "%BACKUP_DIR%\db_!DATE!.sql"

echo Backing up n8n...
docker compose -f "%COMPOSE_FILE%" cp n8n:/home/node/.n8n "%BACKUP_DIR%
8n_!DATE!"

echo Backup complete: %BACKUP_DIR%
goto :eof

:clean
echo Cleaning up...
docker compose -f "%COMPOSE_FILE%" down -v
docker system prune -f
echo Cleaned
goto :eof

:test
echo Testing agent service...
curl -s http://localhost:8000/health | findstr "healthy" >nul && echo Agent: OK || echo Agent: checking...
echo.
echo Testing dev task...
curl -s -X POST http://localhost:8000/dev-task -H "Content-Type: application/json" -d "{"action":"scaffold","task":"Hello World API","tech_stack":"Node.js + Express","project_name":"test-api","tenant_id":"default"}" | findstr "success" >nul && echo Dev task: OK || echo Dev task: checking...
goto :eof

:costs
echo === Execution Costs (Last 30 Days) ===
docker compose -f "%COMPOSE_FILE%" exec -T postgres psql -U ases -d ases_production -c "SELECT task_type, COUNT(*) as runs, SUM(cost_usd) as total_cost, AVG(compute_seconds) as avg_duration FROM executions WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY task_type ORDER BY total_cost DESC;"
goto :eof

:help
:default
echo ASES Multi-Container Operations (Windows)
echo.
echo Usage: run.bat ^<command^> [option]
echo.
echo Commands:
echo   up              Start all services
echo   down            Stop all services
echo   restart [svc]   Restart service(s)
echo   logs [svc]      View logs
echo   ps              Show service status
echo   build           Build images from scratch
echo   pull            Pull latest images
echo   update          Pull and restart
echo   shell [svc]     Open shell in container (default: agent)
echo   db              Open PostgreSQL shell
echo   backup          Manual backup
echo   clean           Stop and remove volumes
echo   test            Test agent health and dev task
echo   costs           Show execution costs
echo.
echo Examples:
echo   run.bat up
echo   run.bat logs agent
echo   run.bat shell n8n
echo   run.bat restart agent
goto :eof
