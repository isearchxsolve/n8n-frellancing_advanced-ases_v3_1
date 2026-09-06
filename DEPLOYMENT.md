# ASES Production Deployment Guide
## Autonomous Software Engineering System

---

## Prerequisites

### Hardware (Minimum)
- **CPU**: 2 vCPU cores
- **RAM**: 4 GB (8 GB recommended for concurrent sandbox execution)
- **Storage**: 40 GB SSD
- **OS**: Ubuntu 22.04 LTS (recommended) or Debian 12

### Software
- Docker 24.0+ with Docker Compose v2
- Git
- A domain name with DNS A record pointing to your server
- SSL certificate (Let's Encrypt auto-configured)

### Accounts & API Keys
| Service | Purpose | Get From |
|---------|---------|----------|
| OpenAI | AI engine | platform.openai.com |
| GitHub | Code storage | github.com/settings/tokens |
| Telegram | Notifications | @BotFather |
| Vercel | Deployments (optional) | vercel.com/account/tokens |
| Upwork | RSS feed | upwork.com (RSS icon on search) |

---

## Step 1: Server Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose v2
sudo apt install docker-compose-plugin

# Verify
docker --version
docker compose version
```

---

## Step 2: Clone & Configure

```bash
# Create project directory
mkdir -p ~/ases && cd ~/ases

# Copy all ASES files here:
# - docker/docker-compose.yml
# - docker/nginx.conf
# - agent_service/ (full directory)
# - database/init.sql
# - n8n_orchestrator.json

# Set permissions
chmod 600 .env
```

Edit `.env` with your actual values (see `.env.example`).

---

## Step 3: SSL Certificate (Let's Encrypt)

```bash
# Install certbot
sudo apt install certbot

# Obtain certificate (replace with your domain)
sudo certbot certonly --standalone -d yourdomain.com

# Certificates will be at:
# /etc/letsencrypt/live/yourdomain.com/fullchain.pem
# /etc/letsencrypt/live/yourdomain.com/privkey.pem

# Copy to docker/ssl/
mkdir -p docker/ssl
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem docker/ssl/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem docker/ssl/
sudo chown -R $USER:$USER docker/ssl
```

---

## Step 4: Launch Infrastructure

```bash
cd ~/ases/docker

# Start all services
docker compose up -d

# Verify all running
docker compose ps

# Check logs
docker compose logs -f agent
docker compose logs -f n8n
```

### Expected Services
| Service | Port | URL |
|---------|------|-----|
| n8n | 5678 | https://yourdomain.com |
| Agent API | 8000 | https://yourdomain.com/api/ |
| PostgreSQL | 5432 | localhost only |
| Redis | 6379 | localhost only |

---

## Step 5: n8n Setup

### 5.1 First Login
1. Visit `https://yourdomain.com`
2. Set up admin account
3. Go to Settings > Credentials

### 5.2 Add Credentials
| Credential | Type | Value |
|------------|------|-------|
| PostgreSQL | postgres | Host: `postgres`, DB: `ases_production` |
| Telegram | telegramApi | Token from @BotFather |
| OpenAI | openAiApi | Your API key |

### 5.3 Import Workflow
1. Workflows > Import from File
2. Select `n8n_orchestrator.json`
3. Activate workflow

### 5.4 Configure Environment Variables in n8n
Settings > External Storage > Environment Variables:
- `AGENT_SERVICE_URL` = `http://agent:8000`
- `UPWORK_RSS_URL` = your RSS URL
- `TELEGRAM_CHAT_ID` = your chat ID
- `TENANT_ID` = `default`

---

## Step 6: Agent Service Verification

```bash
# Test health endpoint
curl https://yourdomain.com/api/health

# Expected: {"status":"healthy","version":"1.0.0","uptime_seconds":...}

# Test lead scoring
curl -X POST https://yourdomain.com/api/process-job \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "test-001",
    "title": "n8n workflow automation needed",
    "description": "Looking for a developer to build n8n workflows with AI integration",
    "link": "https://upwork.com/test",
    "tenant_id": "default"
  }'

# Test dev task
curl -X POST https://yourdomain.com/api/dev-task \
  -H "Content-Type: application/json" \
  -d '{
    "action": "generate_code",
    "task": "Create a REST API with Express and JWT auth",
    "tech_stack": "Node.js + Express",
    "requirements": "Must include login, register, protected routes, and tests",
    "project_name": "test-api",
    "tenant_id": "default"
  }'
```

---

## Step 7: Production Hardening

### 7.1 Firewall
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 7.2 Docker Security
```bash
# Enable Docker Content Trust
export DOCKER_CONTENT_TRUST=1

# Scan images
sudo apt install docker-scan-plugin
docker scan n8nio/n8n:latest
```

### 7.3 Backup Strategy
```bash
# Daily database backup (add to crontab)
0 2 * * * docker exec ases-postgres pg_dump -U ases ases_production > /backup/ases-$(date +\%Y\%m\%d).sql

# n8n workflow backup
0 3 * * * docker cp ases-n8n:/home/node/.n8n /backup/n8n-$(date +\%Y\%m\%d)
```

### 7.4 Monitoring (Optional)
```bash
# Add to docker-compose.yml for Prometheus/Grafana
# See: https://github.com/n8n-io/n8n-docker-caddy
```

---

## Step 8: Troubleshooting

| Issue | Solution |
|-------|----------|
| Agent service won't start | Check `docker compose logs agent` for missing env vars |
| Sandbox creation fails | Ensure Docker socket is mounted: `/var/run/docker.sock` |
| OpenAI rate limits | Add retry logic; use `gpt-4o-mini` for scoring |
| n8n webhook 404 | Verify webhook path matches n8n configuration |
| Database connection refused | Check PostgreSQL is running: `docker compose ps postgres` |
| SSL certificate errors | Run `sudo certbot renew --force-renewal` |

---

## Architecture Diagram

```
Internet
   |
   v
[Nginx:443] ──► [n8n:5678] ──► [Agent:8000] ──► [Docker Sandbox]
   |                |                |                  |
   |                |                |                  v
   |                |                |            [npm install + test]
   |                |                |
   |                |                ▼
   |                |           [PostgreSQL]
   |                |           [Redis]
   |                |
   |                ▼
   |           [Telegram API]
   |           [SendGrid]
   |
   ▼
[GitHub API]
[Vercel API]
```

---

## Cost Estimates

| Component | Monthly Cost (USD) |
|-----------|-------------------|
| VPS (4GB RAM, 2 vCPU) | $20-40 |
| OpenAI API (moderate usage) | $50-150 |
| Domain + SSL | $12/year |
| **Total** | **$70-200/month** |

---

## Support

- n8n Docs: docs.n8n.io
- ASES Issues: GitHub Issues (when repo is public)
- Community: n8n Community Forum

---

*Deploy with confidence. Ship with precision.*
