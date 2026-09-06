#!/bin/bash
# ASES - Autonomous Software Engineering System
# Production Deployment Script
# Run on: Ubuntu 22.04 LTS / Debian 12

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

ASES_DIR="${HOME}/ases"
DOMAIN=""
EMAIL=""

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_command() {
    if ! command -v "$1" &> /dev/null; then
        log_error "$1 is not installed"
        return 1
    fi
    log_success "$1 is installed"
    return 0
}

# =============================================================================
# STEP 0: PRE-FLIGHT CHECKS
# =============================================================================

echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║         ASES - Autonomous Software Engineering System                ║"
echo "║              Production Deployment Script v1.0                       ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""

log_info "Running pre-flight checks..."

# Check root/sudo
if [[ $EUID -eq 0 ]]; then
   log_warn "Running as root. Creating ases user recommended."
fi

# Check OS
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    log_info "Detected OS: $NAME $VERSION_ID"
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        log_warn "This script is tested on Ubuntu/Debian. Proceed with caution."
    fi
else
    log_error "Cannot detect OS"
    exit 1
fi

# Check architecture
ARCH=$(uname -m)
log_info "Architecture: $ARCH"
if [[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" ]]; then
    log_warn "Architecture $ARCH may not be fully supported"
fi

# =============================================================================
# STEP 1: SYSTEM UPDATE & DEPENDENCIES
# =============================================================================

echo ""
log_info "Step 1/8: Updating system and installing dependencies..."

sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# Essential packages
sudo apt-get install -y -qq \
    curl \
    wget \
    git \
    vim \
    nano \
    htop \
    jq \
    unzip \
    ca-certificates \
    gnupg \
    lsb-release \
    software-properties-common \
    apt-transport-https \
    certbot \
    python3-certbot-nginx \
    ufw \
    fail2ban \
    logrotate \
    cron

log_success "System packages installed"

# =============================================================================
# STEP 2: INSTALL DOCKER
# =============================================================================

echo ""
log_info "Step 2/8: Installing Docker..."

if check_command docker; then
    log_info "Docker already installed, skipping..."
else
    log_info "Installing Docker via official script..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "${USER}"
    log_success "Docker installed"
    log_warn "You may need to log out and back in for Docker group changes"
fi

# Install Docker Compose plugin
if ! docker compose version &> /dev/null; then
    log_info "Installing Docker Compose plugin..."
    sudo apt-get install -y -qq docker-compose-plugin
fi

DOCKER_VERSION=$(docker --version)
COMPOSE_VERSION=$(docker compose version)
log_success "$DOCKER_VERSION"
log_success "$COMPOSE_VERSION"

# =============================================================================
# STEP 3: SYSTEM CONFIGURATION
# =============================================================================

echo ""
log_info "Step 3/8: Configuring system..."

# Configure sysctl for containers
cat <<EOF | sudo tee /etc/sysctl.d/99-ases.conf > /dev/null
# ASES - Container optimization
vm.overcommit_memory = 1
kernel.keys.maxkeys = 2000
kernel.keys.maxbytes = 2000000
net.ipv4.ip_forward = 1
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
EOF

sudo sysctl --system > /dev/null 2>&1
log_success "System parameters configured"

# Configure logrotate for Docker
cat <<EOF | sudo tee /etc/logrotate.d/ases-docker > /dev/null
/var/lib/docker/containers/*/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

log_success "Log rotation configured"

# =============================================================================
# STEP 4: FIREWALL SETUP
# =============================================================================

echo ""
log_info "Step 4/8: Configuring firewall (UFW)..."

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Optional: restrict SSH to specific IP
# sudo ufw allow from YOUR_IP to any port 22

sudo ufw --force enable
log_success "Firewall configured"
sudo ufw status verbose

# =============================================================================
# STEP 5: FAIL2BAN
# =============================================================================

echo ""
log_info "Step 5/8: Configuring fail2ban..."

sudo systemctl enable fail2ban
sudo systemctl start fail2ban

# Custom jail for SSH
cat <<EOF | sudo tee /etc/fail2ban/jail.local > /dev/null
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
EOF

sudo systemctl restart fail2ban
log_success "Fail2ban configured"

# =============================================================================
# STEP 6: SSL CERTIFICATE
# =============================================================================

echo ""
log_info "Step 6/8: SSL Certificate setup..."

read -p "Enter your domain name (e.g., ases.yourdomain.com): " DOMAIN
read -p "Enter your email for Let's Encrypt: " EMAIL

if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
    log_error "Domain and email are required"
    exit 1
fi

# Verify DNS resolution
log_info "Verifying DNS for $DOMAIN..."
if ! nslookup "$DOMAIN" &> /dev/null; then
    log_warn "DNS lookup failed for $DOMAIN. Ensure A record points to this server."
    read -p "Continue anyway? (y/N): " CONFIRM
    if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
        exit 1
    fi
fi

# Obtain certificate
log_info "Obtaining SSL certificate from Let's Encrypt..."
sudo certbot certonly --standalone -d "$DOMAIN" --agree-tos --non-interactive --email "$EMAIL"

if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    log_error "Certificate generation failed"
    exit 1
fi

log_success "SSL certificate obtained for $DOMAIN"

# Auto-renewal cron
(crontab -l 2>/dev/null; echo "0 2 * * * certbot renew --quiet") | crontab -
log_success "Auto-renewal configured"

# =============================================================================
# STEP 7: DEPLOY ASES
# =============================================================================

echo ""
log_info "Step 7/8: Deploying ASES..."

# Create directory
mkdir -p "$ASES_DIR"
cd "$ASES_DIR"

# Check if ASES files exist
if [[ ! -f "docker/docker-compose.yml" ]]; then
    log_error "ASES files not found in $ASES_DIR"
    log_info "Please extract the ASES package to $ASES_DIR first:"
    log_info "  unzip ASES-v1.0-production-package.zip -d $ASES_DIR"
    exit 1
fi

# Create .env if not exists
if [[ ! -f ".env" ]]; then
    log_info "Creating .env from template..."
    cp .env.example .env

    # Auto-fill known values
    sed -i "s/DOMAIN=ases.yourdomain.com/DOMAIN=$DOMAIN/g" .env
    sed -i "s/your-secure-password-here/$(openssl rand -base64 32)/g" .env
    sed -i "s/your-secure-db-password/$(openssl rand -base64 24)/g" .env

    log_warn ".env created with auto-generated passwords."
    log_warn "You MUST edit .env and add your API keys before starting:"
    log_warn "  - OPENAI_API_KEY"
    log_warn "  - GITHUB_TOKEN"
    log_warn "  - UPWORK_RSS_URL"
    log_warn "  - TELEGRAM_CHAT_ID"

    read -p "Press Enter to edit .env now, or Ctrl+C to exit and edit manually..."
    ${EDITOR:-nano} .env
fi

# Create SSL directory and copy certificates
mkdir -p docker/ssl
sudo cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" docker/ssl/
sudo cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem" docker/ssl/
sudo chown -R "${USER}:${USER}" docker/ssl
chmod 600 docker/ssl/*.pem

# Update nginx.conf with domain
sed -i "s/\${DOMAIN}/$DOMAIN/g" docker/nginx.conf

# Create required directories
mkdir -p /tmp/ases-sandboxes
mkdir -p /backup/ases

# Pull and start services
log_info "Starting Docker services..."
cd docker
docker compose pull
docker compose up -d

# Wait for services
log_info "Waiting for services to start..."
sleep 10

# Verify
log_info "Checking service status..."
docker compose ps

# Health checks
log_info "Running health checks..."
AGENT_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health || echo "000")
N8N_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5678/healthz || echo "000")

if [[ "$AGENT_HEALTH" == "200" ]]; then
    log_success "Agent service is healthy"
else
    log_warn "Agent service returned HTTP $AGENT_HEALTH"
    log_info "Check logs: docker compose logs agent"
fi

if [[ "$N8N_HEALTH" == "200" || "$N8N_HEALTH" == "401" ]]; then
    log_success "n8n is responding"
else
    log_warn "n8n returned HTTP $N8N_HEALTH"
    log_info "Check logs: docker compose logs n8n"
fi

log_success "ASES deployed successfully!"

# =============================================================================
# STEP 8: POST-DEPLOYMENT
# =============================================================================

echo ""
log_info "Step 8/8: Post-deployment setup..."

# Database initialization
log_info "Initializing database..."
docker compose exec -T postgres psql -U "${POSTGRES_USER:-ases}" -d "${POSTGRES_DB:-ases_production}" < ../database/init.sql 2>/dev/null || \
    log_warn "Database may already be initialized"

# Create backup script
mkdir -p ~/bin
cat <<'EOF' > ~/bin/ases-backup
#!/bin/bash
BACKUP_DIR="/backup/ases"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# Database
cd ~/ases/docker
docker compose exec -T postgres pg_dump -U ases ases_production > "$BACKUP_DIR/db_$DATE.sql"

# n8n workflows
docker cp ases-n8n:/home/node/.n8n "$BACKUP_DIR/n8n_$DATE"

# Cleanup old backups (keep 7 days)
find "$BACKUP_DIR" -name "*.sql" -mtime +7 -delete
find "$BACKUP_DIR" -name "n8n_*" -mtime +7 -exec rm -rf {} +;

echo "Backup completed: $BACKUP_DIR"
EOF
chmod +x ~/bin/ases-backup

# Add to crontab
(crontab -l 2>/dev/null; echo "0 2 * * * ~/bin/ases-backup >> /var/log/ases-backup.log 2>&1") | crontab -
log_success "Daily backup configured (2 AM)"

# =============================================================================
# SUMMARY
# =============================================================================

echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                    DEPLOYMENT COMPLETE                                 ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
echo -e "${GREEN}ASES is now running!${NC}"
echo ""
echo "URLs:"
echo "  n8n Web UI:     https://$DOMAIN"
echo "  Agent API:      https://$DOMAIN/api/"
echo "  Health Check:   https://$DOMAIN/api/health"
echo ""
echo "Next Steps:"
echo "  1. Visit https://$DOMAIN and set up n8n admin account"
echo "  2. Go to Settings > Credentials and add:"
echo "     - PostgreSQL (host: postgres, db: ases_production)"
echo "     - Telegram API (token from @BotFather)"
echo "     - OpenAI API"
echo "  3. Import workflow: Workflows > Import from File > n8n_orchestrator.json"
echo "  4. Activate the workflow"
echo ""
echo "Useful Commands:"
echo "  View logs:        cd ~/ases/docker && docker compose logs -f"
echo "  Agent logs:       cd ~/ases/docker && docker compose logs -f agent"
echo "  Restart:          cd ~/ases/docker && docker compose restart"
echo "  Update:           cd ~/ases/docker && docker compose pull && docker compose up -d"
echo "  Backup now:       ~/bin/ases-backup"
echo "  Database shell:   cd ~/ases/docker && docker compose exec postgres psql -U ases"
echo ""
echo "Security:"
echo "  Firewall:         sudo ufw status"
echo "  Fail2ban:         sudo fail2ban-client status"
echo "  SSL expiry:       sudo certbot certificates"
echo ""
echo -e "${YELLOW}Remember to edit .env and add your API keys if you haven't already!${NC}"
echo ""
