# ASES Setup Script for Windows PowerShell
# Run as Administrator for best results

$ErrorActionPreference = "Stop"

Write-Host "ASES Setup for Windows" -ForegroundColor Cyan
Write-Host "======================" -ForegroundColor Cyan
Write-Host ""

# Check if running as admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin) {
    Write-Warning "Not running as Administrator. Some features may not work."
}

# Check Docker
Write-Host "Checking Docker..." -ForegroundColor Yellow
try {
    $dockerVersion = docker --version
    Write-Host "  [OK] $dockerVersion" -ForegroundColor Green
} catch {
    Write-Error "Docker not found. Install Docker Desktop first."
    exit 1
}

# Check Docker Compose
try {
    $composeVersion = docker compose version
    Write-Host "  [OK] $composeVersion" -ForegroundColor Green
} catch {
    Write-Error "Docker Compose not found."
    exit 1
}

# Check WSL2
$wslInfo = docker info 2>$null | Select-String "WSL2"
if ($wslInfo) {
    Write-Host "  [OK] WSL2 backend detected" -ForegroundColor Green
} else {
    Write-Warning "WSL2 backend not detected. Enable for better Linux container performance."
}

# Create .env if missing
Write-Host ""
Write-Host "Checking environment..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    Write-Host "  Creating .env from template..." -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
    Write-Host "  [WARN] .env created with defaults. EDIT IT with your API keys." -ForegroundColor Yellow
    Write-Host "  Required: OPENAI_API_KEY, GITHUB_TOKEN" -ForegroundColor Yellow

    # Open in notepad
    Start-Process notepad -ArgumentList ".env" -Wait
}

# Create SSL directory
if (-not (Test-Path "ssl")) {
    New-Item -ItemType Directory -Path "ssl" -Force | Out-Null
    Write-Host "  Created ssl/ directory" -ForegroundColor Green
}

# Check SSL certs
if (-not (Test-Path "ssl/fullchain.pem")) {
    Write-Warning "SSL certificates not found in ssl/"
    Write-Host "  For local testing, generate self-signed:" -ForegroundColor Yellow
    Write-Host '    openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout ssl/privkey.pem -out ssl/fullchain.pem' -ForegroundColor Gray
}

# Build and start
Write-Host ""
Write-Host "Building and starting ASES..." -ForegroundColor Yellow
& .\build.bat

Write-Host ""
Write-Host "Starting services..." -ForegroundColor Yellow
docker compose up -d

Write-Host ""
Write-Host "Waiting for services..." -ForegroundColor Yellow
Start-Sleep -Seconds 15

# Health check
Write-Host ""
Write-Host "Health Check:" -ForegroundColor Cyan
$agentHealth = Invoke-RestMethod -Uri "http://localhost:8000/health" -ErrorAction SilentlyContinue
if ($agentHealth.status -eq "healthy") {
    Write-Host "  [OK] Agent service: Healthy" -ForegroundColor Green
} else {
    Write-Warning "Agent service not responding. Check: docker compose logs agent"
}

try {
    $n8nHealth = Invoke-WebRequest -Uri "http://localhost:5678/healthz" -ErrorAction SilentlyContinue
    if ($n8nHealth.StatusCode -in @(200, 401)) {
        Write-Host "  [OK] n8n: Responding" -ForegroundColor Green
    }
} catch {
    Write-Warning "n8n not responding yet. Check: docker compose logs n8n"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  ASES IS RUNNING" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "URLs:" -ForegroundColor White
Write-Host "  n8n UI:       http://localhost" -ForegroundColor White
Write-Host "  Agent API:    http://localhost/api/" -ForegroundColor White
Write-Host "  API Docs:     http://localhost/api/docs" -ForegroundColor White
Write-Host ""
Write-Host "Next Steps:" -ForegroundColor Yellow
Write-Host "  1. Visit http://localhost and set up n8n admin account" -ForegroundColor White
Write-Host "  2. Settings > Credentials > Add PostgreSQL, Telegram, OpenAI" -ForegroundColor White
Write-Host "  3. Workflows > Import from File > n8n_orchestrator.json" -ForegroundColor White
Write-Host "  4. Activate the workflow" -ForegroundColor White
Write-Host ""
Write-Host "Commands:" -ForegroundColor Yellow
Write-Host "  .\run.bat logs        - View logs" -ForegroundColor White
Write-Host "  .\run.bat logs agent  - View agent logs" -ForegroundColor White
Write-Host "  .\run.bat shell       - Open agent shell" -ForegroundColor White
Write-Host "  .\run.bat test        - Test agent" -ForegroundColor White
Write-Host ""
