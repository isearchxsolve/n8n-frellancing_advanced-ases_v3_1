# ═══════════════════════════════════════════════════════════════════════════
# ASES Agent Service - Production Dockerfile
# Multi-stage build for optimized image size
# ═══════════════════════════════════════════════════════════════════════════

# ───────────────────────────────────────────────────────────────────────────
# STAGE 1: Builder
# ───────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ───────────────────────────────────────────────────────────────────────────
# STAGE 2: Runtime
# ───────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS production

LABEL maintainer="ASES Team"
LABEL version="1.0.0"
LABEL description="ASES Agent Service - Multi-Agent Engine"

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONFAULTHANDLER=1

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Docker CLI (for sandbox creation)
    docker.io \
    # Git (for GitHub integration)
    git \
    # SSL certificates
    ca-certificates \
    # Health check
    curl \
    # Cleanup
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -r ases && useradd -r -g ases -m -s /bin/bash ases \
    && usermod -aG docker ases

# Copy Python packages from builder
COPY --from=builder /root/.local /home/ases/.local
ENV PATH=/home/ases/.local/bin:$PATH

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=ases:ases *.py ./
# Create sandbox directory
RUN mkdir -p /app/sandboxes && chown -R ases:ases /app

# Switch to non-root user
USER ases

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Expose port
EXPOSE 8000

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
