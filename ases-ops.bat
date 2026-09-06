@echo off
REM ASES Operations & Monitoring for Windows

setlocal EnableDelayedExpansion

set "CMD=%~1"
if "%CMD%"=="" set "CMD=status"

goto :%CMD% 2>nul || goto :help

:status
echo === ASES Service Status ===
docker compose ps
echo.
echo === Disk Usage ===
wmic logicaldisk get size,freespace,caption 2>nul | findstr /V "Caption"
echo.
echo === Memory ===
systeminfo | findstr "Total Physical Memory"
echo.
goto :eof

:logs
if "%~2"=="" (
    docker compose logs -f
) else (
    docker compose logs -f %~2
)
goto :eof

:restart
if "%~2"=="" (
    docker compose restart
) else (
    docker compose restart %~2
)
goto :eof

:update
echo Updating ASES...
docker compose pull
docker compose up -d
echo Update complete
goto :eof

:backup
set "BACKUP_DIR=%USERPROFILE%ses-backups"
set "DATE=%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "DATE=!DATE: =0!"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

echo Backing up database...
docker compose exec -T postgres pg_dump -U ases ases_production > "%BACKUP_DIR%\db_!DATE!.sql"

echo Backing up n8n...
docker compose cp n8n:/home/node/.n8n "%BACKUP_DIR%
8n_!DATE!"

echo Backup complete: %BACKUP_DIR%
goto :eof

:db-shell
docker compose exec postgres psql -U ases -d ases_production
goto :eof

:clean-sandboxes
echo Cleaning expired sandboxes...
for /f "tokens=*" %%a in ('docker ps -q --filter "name=ases-"') do docker stop %%a
docker system prune -f
echo Cleanup complete
goto :eof

:test-agent
echo Testing agent service...
curl -s http://localhost:8000/health | findstr "healthy" >nul && echo [OK] Agent healthy || echo [FAIL] Agent not responding
echo.
echo Testing dev task...
curl -s -X POST http://localhost:8000/dev-task -H "Content-Type: application/json" -d "{"action":"scaffold","task":"Hello World API","tech_stack":"Node.js + Express","project_name":"test-api","tenant_id":"default"}" | findstr "success" >nul && echo [OK] Dev task working || echo [FAIL] Dev task failed
goto :eof

:costs
echo === Monthly Execution Costs ===
docker compose exec -T postgres psql -U ases -d ases_production -c "SELECT task_type, COUNT(*), SUM(cost_usd), AVG(compute_seconds) FROM executions WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY task_type;"
goto :eof

:help
echo ASES Operations Script (Windows)
echo.
echo Usage: ases-ops.bat ^<command^>
echo.
echo Commands:
echo   status              Show service status
echo   logs [service]      View logs (default: all)
echo   restart [service]   Restart services
echo   update              Pull latest images and restart
echo   backup              Run manual backup
echo   db-shell            Open PostgreSQL shell
echo   clean-sandboxes     Remove old Docker sandboxes
echo   test-agent          Test agent health and dev task
echo   costs               Show execution costs
echo.
echo Examples:
echo   ases-ops.bat status
echo   ases-ops.bat logs agent
echo   ases-ops.bat restart agent
echo   ases-ops.bat test-agent
goto :eof
