#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# ASES Quick Start — Linux / macOS
# One-command setup for local development
# Requires: Docker Engine + Docker Compose v2
# ═══════════════════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

ALL_HEALTHY=1

# ── Helper Functions ───────────────────────────────────────────────────────

print_banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║         ASES — Autonomous Software Engineering System                ║${NC}"
    echo -e "${CYAN}║              Quick Start (Linux / macOS)                             ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_service() {
    local name=$1
    local url=$2
    local check_cmd=$3

    if eval "$check_cmd" > /dev/null 2>&1; then
        echo -e "   ${GREEN}✓${NC} $name"
        return 0
    else
        echo -e "   ${YELLOW}✗${NC} $name (still starting?)"
        ALL_HEALTHY=0
        return 1
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────

print_banner

# ── Check Docker ───────────────────────────────────────────────────────────
log_info "Verifying Docker installation..."

if ! command -v docker &> /dev/null; then
    echo ""
    log_error "Docker not found."
    echo ""
    echo "Please install Docker Engine:"
    echo "  Ubuntu/Debian:  curl -fsSL https://get.docker.com | sh"
    echo "  macOS:          brew install --cask docker"
    echo "  Other:          https://docs.docker.com/engine/install/"
    echo ""
    echo "After installation:"
    echo "  sudo usermod -aG docker \$USER"
    echo "  newgrp docker"
    echo "  # Then re-run this script"
    echo ""
    exit 1
fi

DOCKER_VER=$(docker --version)
log_ok "Docker found: $DOCKER_VER"

if ! docker info > /dev/null 2>&1; then
    echo ""
    log_error "Docker daemon is not running or you lack permissions."
    echo ""
    echo "Fix:"
    echo "  sudo systemctl start docker    # Linux"
    echo "  open -a Docker                 # macOS"
    echo ""
    exit 1
fi
log_ok "Docker daemon is running"

if ! docker compose version > /dev/null 2>&1; then
    log_error "Docker Compose v2 not found."
    echo "Install: sudo apt install docker-compose-plugin"
    exit 1
fi
log_ok "Docker Compose available"

# ── Check OS ───────────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
log_info "Detected: $OS ($ARCH)"

# ── Environment Setup ────────────────────────────────────────────────────────
echo ""
log_info "Checking environment configuration..."

if [[ ! -f ".env" ]]; then
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  ACTION REQUIRED: Configure .env file                                ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    cp .env.example .env

    echo -e "${YELLOW}The .env file has been created with default values.${NC}"
    echo -e "${YELLOW}You MUST edit it and add your API keys before continuing.${NC}"
    echo ""
    echo "REQUIRED keys to add:"
    echo "  1. OPENAI_API_KEY     — https://platform.openai.com/api-keys"
    echo "  2. GITHUB_TOKEN       — https://github.com/settings/tokens"
    echo ""
    echo "OPTIONAL keys:"
    echo "  3. VERCEL_TOKEN       — For deployments"
    echo "  4. TELEGRAM_BOT_TOKEN — For notifications (get from @BotFather)"
    echo "  5. UPWORK_RSS_URL     — For lead discovery"
    echo ""

    # Open in editor
    if [[ -n "$EDITOR" ]]; then
        $EDITOR .env
    elif command -v nano &> /dev/null; then
        nano .env
    elif command -v vim &> /dev/null; then
        vim .env
    else
        echo "Please edit .env manually, then run this script again."
        exit 1
    fi

    echo ""
    log_info "Checking if .env was configured..."
    if ! grep -qE "(sk-|ghp-)" .env 2>/dev/null; then
        log_warn "API keys not detected in .env."
        log_warn "Services may fail to start without valid keys."
        read -p "Continue anyway? (y/N): " CONFIRM
        if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
            exit 1
        fi
    fi
else
    log_ok ".env file exists"
fi

# ── SSL Certificates ───────────────────────────────────────────────────────
mkdir -p ssl

if [[ ! -f "ssl/fullchain.pem" ]]; then
    echo ""
    log_info "SSL certificates not found. Generating self-signed for local testing..."

    if command -v openssl &> /dev/null; then
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout ssl/privkey.pem \
            -out ssl/fullchain.pem \
            -subj "/C=US/ST=Local/L=Local/O=ASES/CN=localhost" \
            2>/dev/null
        log_ok "Self-signed certificates generated in ssl/"
    else
        log_warn "OpenSSL not found. Please install OpenSSL or provide your own certificates."
        log_warn "Place fullchain.pem and privkey.pem in the ssl/ directory."
    fi
fi

# ── Start Services ─────────────────────────────────────────────────────────
echo ""
log_info "Starting ASES services..."
echo "         This may take 2-3 minutes on first run (images will be downloaded)."
echo ""

docker compose up -d

if [[ $? -ne 0 ]]; then
    echo ""
    log_error "Failed to start services."
    log_info "Check: docker compose logs"
    exit 1
fi

# ── Wait for initialization ────────────────────────────────────────────────
echo ""
log_info "Waiting for services to initialize (30 seconds)..."
sleep 30

# ── Health Checks ──────────────────────────────────────────────────────────
echo ""
log_info "Running health checks..."
echo ""

check_service "Agent Service     : http://localhost:8000/health" \
    "http://localhost:8000/health" \
    "curl -s http://localhost:8000/health | grep -q healthy"

check_service "n8n Orchestrator  : http://localhost:5678/healthz" \
    "http://localhost:5678/healthz" \
    "curl -s -o /dev/null -w '%{http_code}' http://localhost:5678/healthz | grep -qE '200|401'"

check_service "Nginx Proxy       : http://localhost/" \
    "http://localhost/" \
    "curl -s -o /dev/null -w '%{http_code}' http://localhost/ | grep -qE '200|301|302'"

check_service "PostgreSQL        : localhost:5432" \
    "localhost:5432" \
    "docker compose exec -T postgres pg_isready -U ases"

check_service "Redis             : localhost:6379" \
    "localhost:6379" \
    "docker compose exec -T redis redis-cli ping | grep -q PONG"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════════╗${NC}"

if [[ $ALL_HEALTHY -eq 1 ]]; then
    echo -e "${CYAN}║  ${GREEN}✓ ALL SERVICES HEALTHY${CYAN}                                               ║${NC}"
else
    echo -e "${CYAN}║  ${YELLOW}⚠ SOME SERVICES STILL STARTING${CYAN}                                     ║${NC}"
fi

echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${BOLD}Access Points:${NC}"
echo "  ┌────────────────────────────────────────────────────────────────────┐"
echo "  │  n8n Web UI       : http://localhost                             │"
echo "  │  Agent API        : http://localhost/api/                        │"
echo "  │  API Documentation: http://localhost/api/docs                     │"
echo "  │  PostgreSQL       : localhost:5432  (user: ases)                 │"
echo "  │  Redis            : localhost:6379                                │"
echo "  └────────────────────────────────────────────────────────────────────┘"
echo ""

if [[ $ALL_HEALTHY -eq 0 ]]; then
    log_info "Some services are still initializing."
    echo "  Check status:  docker compose ps"
    echo "  View logs:     docker compose logs [service_name]"
    echo ""
fi

echo -e "${BOLD}Next Steps:${NC}"
echo "  1. Visit http://localhost in your browser"
echo "  2. Set up n8n admin account (first visit)"
echo "  3. Settings > Credentials > Add:"
echo "     • PostgreSQL (host: postgres, db: ases_production)"
echo "     • Telegram API (token from @BotFather)"
echo "     • OpenAI API"
echo "  4. Workflows > Import from File > n8n_orchestrator.json"
echo "  5. Activate the workflow"
echo ""

echo -e "${BOLD}Useful Commands:${NC}"
echo "  ./run.sh logs        - View all logs"
echo "  ./run.sh logs agent  - View agent logs"
echo "  ./run.sh shell       - Open agent shell"
echo "  ./run.sh test        - Test agent service"
echo "  ./run.sh backup      - Manual backup"
echo "  ./run.sh costs       - Show execution costs"
echo ""

echo -e "${BOLD}To stop:${NC}"
echo "  ./run.sh down"
echo ""
