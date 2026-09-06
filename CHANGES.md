# ASES v2.0 — Changes from v1.0

All issues identified in the code audit have been resolved.

---

## Fix 1 — Job queue (critical)

**Problem**: `process-job` and `dev-task` awaited the full agent loop inline on the
API thread. Request #2 blocked behind request #1 for minutes.

**Fix**: New `queue.py` using **RQ + Redis**.
- API endpoints enqueue and return `202 Accepted` with an `execution_id` in < 10ms.
- New `worker.py` runs as a separate process (or multiple) and executes jobs.
- New `GET /jobs/{execution_id}` endpoint for polling status and result.
- Scale workers: `docker-compose up --scale worker=4`
- New `worker` service added to `docker-compose.yml` with `replicas: 2` default.

Files changed: `queue.py` (new), `worker.py` (new), `main.py`, `docker-compose.yml`

---

## Fix 2 — Sandbox state persistence (critical)

**Problem**: `ACTIVE_SANDBOXES` was an in-process Python dict. Worker restart = all
sandbox records lost, Docker containers keep running, memory leaks indefinitely.

**Fix**: New `sandbox_registry` Postgres table. All sandbox create/destroy/cleanup
operations read and write the DB.
- `create_sandbox` registers to DB immediately after container start.
- `cleanup_sandbox` deregisters from DB after container stop.
- `reconcile_sandboxes_on_startup()` called at app startup — compares `sandbox_registry`
  against `docker ps`, kills ghost containers from previous runs.
- `cleanup_expired_sandboxes` now queries DB instead of the in-process dict — works
  correctly across multiple worker processes.

Files changed: `sandbox.py` (rewritten), `db.py` (new), `database/init.sql`

---

## Fix 3 — Tenant config from Postgres (medium)

**Problem**: `get_tenant_config` returned hardcoded defaults for every request, ignoring
whatever was stored in the `tenants` table.

**Fix**: `db.get_tenant_config_from_db()` fetches the `config JSONB` column from
`tenants` and builds a `TenantConfig` from it. Falls back to safe defaults for any
missing keys. Auto-creates a default tenant row on first use so the system
self-bootstraps without manual DB setup.

Files changed: `db.py` (new), `main.py`, `worker.py`

---

## Fix 4 — Stack-aware test commands (medium)

**Problem**: `run_command(sandbox_id, "npm install && npm test")` was hardcoded for
all stacks. Python, Go, Rust projects would fail on line 1.

**Fix**: `sandbox.py` exports `get_test_command(tech_stack)` which maps each stack to
the correct install + test invocation:

| Stack          | Command                                              |
|----------------|------------------------------------------------------|
| Node.js/Express| `npm install && npm test`                            |
| React          | `npm install && npm test -- --watchAll=false`        |
| Next.js        | `npm install && npm run build`                       |
| Python/FastAPI | `pip install -r requirements.txt && python -m pytest`|
| Django         | `pip install -r requirements.txt && python manage.py test` |
| Go             | `go test ./...`                                      |
| Rust           | `cargo test`                                         |

`create_sandbox` also selects the correct Docker image per stack.

Files changed: `sandbox.py`, `agent_loop.py`

---

## Fix 5 — CRM webhook implementation (medium)

**Problem**: `_process_crm_async` was `await asyncio.sleep(0.1)` plus a log line.
Four documented actions (`new_client`, `update_status`, `add_note`, `invoice_paid`)
were stubs.

**Fix**: `worker.py` now routes `crm_*` task types to `_handle_crm()` which writes
to the `clients`, `client_notes`, and `payments` tables. All four actions are
implemented. `invoice_paid` also promotes the client status to `active`.

Files changed: `worker.py`, `main.py`

---

## Fix 6 — CORS locked down (low)

**Problem**: `allow_origins=["*"]` in `main.py`.

**Fix**: Origins read from `CORS_ALLOWED_ORIGINS` env var (comma-separated list).
Default is `http://localhost:3000,http://localhost:5678`. Set to your actual domain
in production. Added to `docker-compose.yml` agent environment block and `.env.example`.

Files changed: `main.py`, `docker-compose.yml`, `.env.example`

---

## Architecture after v2

```
n8n / client
    │  POST /process-job or /dev-task
    ▼
FastAPI (main.py)           ← returns 202 + execution_id in < 10ms
    │  enqueue_agent_job()
    ▼
Redis queue (RQ)
    │
    ▼
Worker process(es) (worker.py)
    │  run_multi_agent() / _handle_crm()
    ▼
Postgres  ←── execution result, tenant config, sandbox registry, CRM data
```

Client polls `GET /jobs/{execution_id}` until `status == "complete"` or `"failed"`.
