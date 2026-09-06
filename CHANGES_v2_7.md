# ASES v2.7 — Gap Fixes: Vector Memory, Scored Journal, Visual Gate, Interface Cache

This release closes the four architectural gaps identified in the v2.6 post-mortem.
No external API changes. All fixes are additive or drop-in replacements.

---

## Gap 1 — Vector memory search (`vector_memory.py`)

**Before:** `retrieve_memory_patterns` used `ILIKE` substring matching. Zero hits on
paraphrase — "build a JWT auth service" matched nothing stored as "implement
authentication with tokens".

**After:** `retrieve_memory_patterns_vector` embeds the incoming task with
`text-embedding-3-small` (~$0.00002/call) and runs a pgvector cosine similarity query
with a 0.70 similarity floor. Falls back to ILIKE silently if OpenAI or pgvector
unavailable — zero regression risk.

`store_memory_pattern_vector` embeds and persists on every successful job. Existing
rows without embeddings still serve via the ILIKE fallback.

**Files changed:** `vector_memory.py` (new), `agent_loop.py`, `requirements.txt`,
`docker-compose.yml` (postgres → pgvector/pgvector image),
`database/migration_v2_7.sql`

---

## Gap 2 — Scored constraint journal (`iteration_journal.py`)

**Before:** Every passing iteration appended 3–5 constraints to a flat list. On a
10-iteration run: 50 constraints injected with equal weight. The coder learned to
ignore the noise.

**After:** Each constraint carries `(confirmed, violated, score)`. Score formula:
`confirmed × 1.0 − violated × 2.5`. Only the top 8 constraints by score are
injected. Constraints below −3.0 are pruned entirely.

Violations are detected via `journal.penalise_violated(diff_report.broken_imports)`,
called automatically after `SemanticDiffer.diff()` runs when regressions are found.
Constraints implicated in a regression have their score penalised 2.5× per violation.

Context block now shows `[KEEP/HIGH]`, `[KEEP/MED]`, `[KEEP/LOW]` confidence tags
so the coder can weight them appropriately.

**Files changed:** `iteration_journal.py` (full rewrite, API-compatible),
`agent_loop.py`

---

## Gap 3 — Last-mile visual gate (`visual_reviewer.py`)

**Before:** Visual review (gpt-4o vision, ~$0.02/call + 8–10s latency) fired on
every LLM-approved iteration, even iteration 2 of 5 when rich text errors existed.

**After:** Two gate conditions added to `_should_run_visual()`:

1. **Not last-mile:** Skip if `max_iterations - iteration > LAST_MILE_RESERVE (2)`.
   On a 5-iteration job: visual review only eligible on iterations 4 and 5.
2. **Rich errors exist:** Skip if `len(previous_errors) > 200 chars`.
   If the coder already has structured text feedback, the vision call adds nothing.

Net result: 0–1 vision calls per job instead of up to max_iterations.
`visual_reviewer()` now accepts `iteration`, `max_iterations`, `previous_errors`
kwargs (default to permissive values for back-compat).

**Files changed:** `visual_reviewer.py`, `agent_loop.py`

---

## Gap 4 — Cross-job interface cache (`interface_cache.py`)

**Before:** `SemanticDiffer` built its interface map from the current job's files.
On iteration 1, no prior baseline existed — regressions vs the tenant's typical
interface shape were invisible until tests failed.

**After:** `interface_signatures` table persists `(tenant_id, tech_stack, file_pattern, exports)`
across jobs. On job start, `load_interface_signatures()` fetches the tenant's known
interface shapes. `build_warm_baseline()` synthesizes a minimal "previous iteration"
file set from the cache, giving the differ a non-empty baseline on iteration 1.

On successful completion, `store_interface_signatures()` upserts the new signatures
(with `hit_count` tracking which patterns are most reliable).

**Files changed:** `interface_cache.py` (new), `agent_loop.py`,
`database/migration_v2_7.sql`

---

## Migration instructions

```bash
# 1. Update postgres image (pgvector requires the extension)
#    docker-compose.yml already updated to pgvector/pgvector:pg16

# 2. Run DB migration
psql $DATABASE_URL -f database/migration_v2_7.sql

# 3. Rebuild agent service
docker compose build agent_service

# 4. Restart
docker compose up -d
```

No data loss. Existing `code_patterns` rows continue to work via ILIKE fallback
until their embeddings are populated by the next successful job run.
