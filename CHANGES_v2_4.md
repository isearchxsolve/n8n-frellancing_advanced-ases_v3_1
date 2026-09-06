# ASES v2.4 — Enterprise-Grade Upgrades

## 🟢 What's new in v2.4

### 1. Global Scheduler / Priority Load Balancer (`scheduler.py`)
- **4 priority queues**: P0 critical → P1 high → P2 normal → P3 low
- Workers drain highest non-empty queue first (RQ ordered queue list)
- Plan-based routing: enterprise → high, pro → normal, free → low
- Resource-aware: CPU load guard blocks scale-up when CPU > 80%
- Redis atomic counters track active jobs across all workers
- Replace `queue.py`'s `enqueue_agent_job()` with `scheduler.enqueue_with_priority()`

### 2. Autoscaler (`autoscaler.py`)
- Polls queue depths every 15s (configurable via `AUTOSCALER_POLL_INTERVAL`)
- Scales UP when `total_queued / workers > AUTOSCALER_SCALE_UP_THRESHOLD` (default 3.0)
- Scales DOWN after `AUTOSCALER_SCALE_DOWN_GRACE` seconds of idle (default 60s)
- Respects `AUTOSCALER_MIN_WORKERS` (1) and `AUTOSCALER_MAX_WORKERS` (8)
- CPU guard: never adds workers when host CPU > `AUTOSCALER_CPU_MAX` (default 80%)
- Runs as a single sidecar process in docker-compose (`replicas: 1`)
- Auto-respawns minimum workers if any crash

### 3. Observability (`observability.py`)
- **Prometheus metrics** at `GET /metrics` (scraped by Prometheus/Grafana)
  - `ases_jobs_total` — by tenant, task_type, status
  - `ases_job_duration_seconds` — histogram by task_type
  - `ases_job_cost_usd` — histogram by tenant + task_type
  - `ases_tokens_total` — by tenant + model
  - `ases_llm_calls_total` / `ases_llm_latency_seconds` — by model + agent
  - `ases_active_jobs`, `ases_queue_depth`, `ases_worker_count`, `ases_active_sandboxes`
  - `ases_billing_enforced_total` — limit hits by tenant + reason
- **OpenTelemetry tracing** via OTLP (Jaeger / Tempo / Grafana Cloud)
  - Enable by setting `OTEL_EXPORTER_OTLP_ENDPOINT`
  - Auto-instruments FastAPI, httpx, asyncpg
  - `trace_llm_call()` context manager for per-call latency spans
- Zero-config: if `prometheus-client` or OTel packages absent, observability degrades gracefully

### 4. Active Billing Enforcement (`billing.py`)
- `BillingFence` replaces the old manual `if total_tokens > budget: return` blocks
- **Pre-flight check**: queries daily + monthly aggregate spend BEFORE any LLM call
- **Per-job checkpoint**: called between every agent iteration with current cost
- **Soft warning** at 80% of job limit (logged + Prometheus counter)
- **Hard kill** at 100% — raises `BillingLimitError`, sandbox cleaned up cleanly
- Plan-level limits enforced: free ($0.50/day), pro ($10/day), enterprise ($200/day)
- Spend persisted to `billing_events` table for billing reports
- New DB table: `billing_events` + `billing_daily` view (see `database/init.sql`)

### 5. Static Analysis Reviewer (`static_reviewer.py`)
Replaces purely heuristic LLM reviewer with a 4-layer pipeline:

| Layer | Tool | What it catches |
|-------|------|----------------|
| 1 — AST | Python `ast` / regex (JS) | Syntax errors, `eval()`, bare `except`, XSS patterns |
| 2 — Lint | `ruff` (Python) / `eslint` (JS) | Style, unused vars, type issues, formatting |
| 3 — Vuln scan | `pip-audit` (Python) / `npm audit` (JS) | Known CVEs in dependencies |
| 4 — LLM gate | `reviewer_agent` (existing) | Code quality, logic, completeness |

- Static layers run in a thread pool (non-blocking)
- Files are written to a temp dir — no sandbox needed
- Layer 4 (LLM) only runs when layers 1–3 pass — saves tokens on bad code
- Returns same dict shape as old `reviewer_agent` (backward compatible)

## Migration notes

1. **New env vars** (all optional, sensible defaults):
   ```
   AUTOSCALER_MIN_WORKERS=1
   AUTOSCALER_MAX_WORKERS=8
   AUTOSCALER_SCALE_UP_THRESHOLD=3.0
   AUTOSCALER_CPU_MAX=80.0
   OTEL_EXPORTER_OTLP_ENDPOINT=      # blank = tracing disabled
   OTEL_SERVICE_NAME=ases-agent-service
   ```

2. **DB migration**: run the new stanza at the bottom of `database/init.sql`
   (creates `billing_events` table + `billing_daily` view).

3. **docker-compose**: new `autoscaler` service added — no action needed,
   it starts automatically.

4. **requirements.txt**: new packages added. Rebuild image:
   ```
   docker-compose build agent
   ```
