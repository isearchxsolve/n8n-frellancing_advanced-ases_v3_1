# ASES v3.0 — Learned Failure Classifier + Surgical Design Patch

Two features that make the design-level failure triage smarter and cheaper.
Both are backwards-compatible: the system degrades gracefully to v2.10 behaviour
when the classifier is undertrained or the patch cannot be applied.

New file: `failure_classifier.py`
New migration: `database/migration_v3_0.sql`

---

## Feature 1 — Surgical design spec patch (`design_regenerator.py`)

**Problem (v2.10):**
When a visual review fails with a design-level issue (e.g. "modal z-index too low"),
`regenerate_design_spec` rewrites the entire spec — ~2500 tokens, ~5–8 seconds.
Most failures affect a single property on a single component. Rewriting the whole
spec is wasteful and risks silently changing unrelated decisions that were working.

**Fix — `patch_design_spec()` runs before full regen:**
The model is asked to output only a *delta*: a minimal JSON fragment specifying
which field(s) to overwrite and their new values, using dot-notation paths:

```json
{
  "target": "component",
  "component_name": "Modal",
  "patches": {
    "layout_rules[0]": "position: fixed",
    "layout_rules[1]": "z-index: 9999"
  },
  "rationale": "Modal was rendered inside a stacking context; fixed positioning with high z-index escapes it"
}
```

The patch is merged onto a deep copy of the original spec via `_apply_patches()`,
which handles dot-notation and array-index paths without eval.

If the model returns `{"target": "CANNOT_PATCH"}` or the patch is invalid,
`patch_design_spec()` returns `None` and the caller falls through to full regen.

**Cost comparison:**

| Operation           | Tokens  | Latency |
|---------------------|---------|---------|
| Full regen (v2.10)  | ~2 500  | ~6 s    |
| Patch (v3.0, hit)   | ~400    | ~1 s    |
| Patch (v3.0, miss)  | ~400    | ~1 s    |
| Regen (v3.0 fallback)| ~2 500 | ~6 s    |

Expected patch hit rate: ~60–70% of single-property failures. On a 5-iteration
frontend job with one visual failure, this saves ~2100 tokens and ~5 seconds.

**Call order in `agent_loop.py`:**
1. `patch_design_spec()` — cheap, targeted
2. `regenerate_design_spec()` — expensive, full rewrite (only if patch returns None)

**Files changed:** `design_regenerator.py`, `agent_loop.py`

---

## Feature 2 — Per-tenant learned failure classifier (`failure_classifier.py`, `agent_loop.py`)

**Problem (v2.10):**
`is_design_level_failure()` uses a keyword scoring heuristic with fixed weights.
"modal is clipped" always scores 0.4. But whether clipping is design-level or
code-level depends on the tenant's stack — a React-portal codebase fixes clipping
in code; a CSS-only codebase needs a spec change. The keyword weights are
miscalibrated for every tenant except a hypothetical average one.

Over time, this causes:
- Over-regen on tenants whose coders handle positioning well (wasted tokens)
- Under-regen on tenants whose specs consistently omit z-index (silent failures)

**Fix — logistic regression classifier trained from journal data:**

Architecture:
- Features: TF-IDF bag-of-words (top 300 vocab) + 5 heuristic prior features
  derived from `score_design_failure()` sub-scores
- Labels: generated automatically by `agent_loop.py`
  - `label=1` when design regen is triggered for a failure description
  - (code-level failures are not labeled yet — conservative approach)
- Training: `train_classifier_from_journal()`, fire-and-forget after each
  approved iteration via `asyncio.create_task()`
- Inference: `is_design_level_failure_learned()`, called per-failure with
  graceful `None` return on cold start / insufficient data

**Cold-start behaviour (< 20 labeled samples):**
Returns `None` → caller falls back to `is_design_level_failure()` heuristic.
No cliff edge. System behaves identically to v2.10 until enough data accumulates.

**No new dependencies:**
Pure Python + stdlib math. Logistic regression implemented in ~20 lines using
SGD. No sklearn, no scipy. Works in the existing Docker image without rebuilding.

**Persistence:**
Model weights stored as JSONB in `tenant_classifiers` (new table).
Training data stored in `classifier_training_data` (new table).
Both tables created by `database/migration_v3_0.sql`.

**Files changed:** `agent_loop.py`
**Files added:** `failure_classifier.py`

---

## DB changes in `interaction_reviewer.py` (v3.0 also ships v2.10 Playwright fix)

The 800 ms post-action sleep (`await page.wait_for_timeout(800)`) is replaced
with per-action state-based readiness waits:

| Action    | Pre-wait                       | Post-wait                          |
|-----------|--------------------------------|------------------------------------|
| `click`   | visible + enabled              | `domcontentloaded`                 |
| `keyboard`| —                              | `domcontentloaded`                 |
| `type`    | visible + enabled on field     | `networkidle` (debounce/validation)|
| `hover`   | visible                        | `domcontentloaded`                 |
| `focus`   | visible                        | — (synchronous)                    |
| `touch`   | visible                        | `domcontentloaded`                 |

The `enabled` check is the key one: in React/Vue, an element becomes enabled only
after hydration attaches the event handler — this is the deterministic signal the
800 ms sleep was approximating heuristically.

**Files changed:** `interaction_reviewer.py`

---

## Migration instructions

```bash
# 1. Run the migration (safe — IF NOT EXISTS, no existing tables touched)
psql $DATABASE_URL -f database/migration_v3_0.sql

# 2. Rebuild and restart
docker compose build agent_service
docker compose up -d
```

The classifier activates automatically per tenant after 20 labeled samples
accumulate. No manual intervention required.

To inspect classifier status for a tenant:

```sql
SELECT tenant_id,
       (classifier->>'n_samples')::int   AS samples,
       (classifier->>'train_accuracy')::float AS accuracy,
       updated_at
FROM tenant_classifiers
ORDER BY updated_at DESC;
```
