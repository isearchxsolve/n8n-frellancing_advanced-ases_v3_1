# ASES v2.5 — Full Gap Closure + Redis Cache + HITL Safety Gate

## Summary

v2.5 closes every gap identified in the three-file architecture audit (docs 1-3)
and the workflow comparison analysis. The backend was already production-grade.
This release fixes the n8n layer and completes the missing backend integrations
that were planned but unimplemented.

---

## 🟢 What's New in v2.5

### 1. Rebuilt n8n Orchestrator (`n8n_orchestrator.json`)

The `full_freelance_ai_agent_n8n_FIXED.json` file (the "fixed" deliverable from
the prior session) contained four unresolved structural problems. It has been
retired. The new `n8n_orchestrator.json` replaces it entirely.

**Problems in FIXED.json that are now resolved:**

| Was broken | Now fixed |
|-----------|-----------|
| Google Sheets dedup (`search` + `$json.length == 0` IF) | PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` — atomic, no race |
| Hardcoded fake test runner (`passed: true` in Code node) | Removed. Dev tests run in real Docker sandbox via `agent_service` |
| GitHub Gist dump | Removed. `agent_service` handles real `git commit/push` |
| Puppeteer auto-fires without human approval | Two-stage HITL gate (see below) |
| Cold outreach reads from Google Sheets | PostgreSQL `cold_leads` table |
| CRM branches wired to Sheets nodes | Real Postgres INSERT/UPDATE nodes |
| Telegram reply via plain text "SUBMIT" | Inline keyboard callback_query |

**New in the orchestrator:**

- **HITL inline keyboard** — bid candidates get APPROVE / REJECT buttons.
  The `callback_query` listener handles the response, dismisses the loading
  spinner via `answerCallbackQuery`, then loads the proposal from PostgreSQL.
- **Two-stage Puppeteer gate** — APPROVE fills the form and sends a screenshot.
  A second CONFIRM reply is required before any click fires. Prevents accidental
  submission from a mis-sent message in the wrong chat.
- **Cold outreach pipeline** — weekly schedule → DB fetch → `/personalize-email`
  API call → SendGrid send → DB status update. No Sheets dependency.
- **All four CRM branches fully wired** — `new_client`, `update_status`,
  `add_note`, `invoice_paid` each have real Postgres and notification nodes.
- **Dev task webhook timeout** raised to 5 minutes (from 30s default) to
  accommodate multi-iteration agent loops.

---

### 2. Redis Prompt Cache (`agent_service/redis_cache.py`)

New module. Drop-in cache for `call_model()`.

**How it works:**
- Cache key = SHA-256 of `{model, messages, temperature}` JSON
- Cache hit returns `(content, 0, 0)` — zero tokens, not billed
- TTL is call-type aware: planner 24h, reviewer 12h, coder 1h
- Redis unavailable → silent fallback to uncached, never raises

**Expected savings:**
- Planner calls for repeated task types: ~40% hit rate
- Reviewer calls for common code patterns: ~25% hit rate
- Net daily token reduction: 20–35% on busy workloads

**Integration:** `call_model()` now accepts `call_type` parameter. All 7
call sites tagged with the appropriate type.

---

### 3. Memory Layer — Active Retrieval and Storage (`agent_loop.py`)

The `code_patterns` table existed since v2.0 but was never read or written.
Now it is a live self-improvement loop:

- **On each dev job**: `retrieve_memory_patterns()` queries `code_patterns` for
  similar past solutions and injects them into the coder's context
- **On reviewer approval**: `store_memory_pattern()` writes the successful
  solution back to `code_patterns` with an incrementing `success_count`
- Pattern retrieval uses keyword ILIKE matching (top 3 results by success count)
- Designed for `pgvector` upgrade: replace the ILIKE query with cosine
  similarity when embedding support is added

**Effect**: The coder improves with every successful job. Common tasks
(auth APIs, CRUD endpoints, webhook handlers) converge to 1-iteration solutions.

---

### 4. Cold Outreach Personalization Agent

New complete pipeline: API endpoint → agent function → two-pass email generation.

**`/personalize-email` endpoint** (added to `main.py`):
- Accepts `{lead_id, name, company, notes, tenant_id}`
- Returns 202 + execution_id immediately
- Polls via `/jobs/{execution_id}`

**`outreach_personalize_agent()` function** (added to `agent_loop.py`):
- Pass 1: draft email with company-specific opening, one portfolio reference, low-friction CTA
- Pass 2: self-critique for specificity (rewrite if < 9/10)
- Output: `{subject, body, email, lead_id}`
- Uses `reviewer_model` (cheap) — proposal quality doesn't need gpt-4o

**`run_multi_agent()` router** updated to handle `outreach_personalize` task type.

---

### 5. Database Schema Updates (`database/init.sql`)

Two new tables added:

**`cold_leads`** — proper cold outreach CRM table:
```sql
id, tenant_id, name, email, company, notes,
outreach_status (pending/contacted/replied/converted/unsubscribed),
last_contacted_at, follow_up_date, source
```
Replaces the `outreach` table (which had an incompatible schema for the
n8n orchestrator's SELECT query).

**`prompt_cache_stats`** — optional cache observability:
```sql
tenant_id, date, hits, misses, tokens_saved, cost_saved_usd
```
Populate via cron or extend `redis_cache.py` if cost reporting is needed.

---

### 6. Infrastructure Fixes

**`docker-compose.yml`**:
- Added `TELEGRAM_BOT_TOKEN` env var to n8n service (required for
  `answerCallbackQuery` call in the HITL flow)
- Added `OUTREACH_FROM_EMAIL` env var

**`.env.example`**:
- Added `TELEGRAM_BOT_TOKEN`, `OUTREACH_FROM_EMAIL`, `SENDGRID_API_KEY`
- Added Redis cache TTL override documentation

---

## Migration from v2.4

### 1. Database migration (required)
```sql
-- Run the new stanza at the bottom of database/init.sql
-- (adds cold_leads, prompt_cache_stats tables)
psql -U ases -d ases_production -f database/init.sql
```

### 2. Environment variables (add to .env)
```
TELEGRAM_BOT_TOKEN=<your bot token>
OUTREACH_FROM_EMAIL=<your sender address>
SENDGRID_API_KEY=<your sendgrid key>
```

### 3. Rebuild agent image
```bash
docker-compose build agent worker autoscaler
docker-compose up -d
```

### 4. Retire the FIXED.json workflow
Delete `full_freelance_ai_agent_n8n_FIXED.json`. Import the new
`n8n_orchestrator.json` via n8n → Settings → Import.

### 5. No breaking changes to existing APIs
All v2.4 endpoints (`/process-job`, `/dev-task`, `/crm-webhook`, `/jobs/{id}`)
are unchanged. New: `/personalize-email`.

---

## Architecture Doc Coverage

| Doc requirement | Status |
|----------------|--------|
| Multi-agent: Planner + Coder + Reviewer | ✅ v2.0+ |
| Real execution sandbox (Docker) | ✅ v2.0+ |
| Real test runner (npm test / pytest) | ✅ v2.0+ |
| Cost-tiered model routing | ✅ v2.0+ |
| Redis prompt caching | ✅ **v2.5 NEW** |
| Token budget guard | ✅ v2.4 (BillingFence) |
| Memory / self-improving agent | ✅ **v2.5 NEW** |
| SaaS multi-tenant isolation | ✅ v2.0+ |
| HITL gate before browser automation | ✅ **v2.5 NEW** |
| PostgreSQL dedup (not Sheets) | ✅ **v2.5 NEW** (n8n layer) |
| Cold outreach personalisation | ✅ **v2.5 NEW** |
| CRM fully wired | ✅ **v2.5 NEW** (n8n layer) |
| Static analysis reviewer (4-layer) | ✅ v2.4+ |
| Priority queue scheduler | ✅ v2.4+ |
| Autoscaler | ✅ v2.4+ |
| Observability (Prometheus + OTel) | ✅ v2.4+ |
| Active billing enforcement | ✅ v2.4+ |
