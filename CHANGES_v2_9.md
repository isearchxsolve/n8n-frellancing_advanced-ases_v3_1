# ASES v2.9 — Five Pending Fixes from v2.8 Audit

All five issues identified in the v2.8 evaluation are resolved in this release.
No external API changes. No new dependencies.

---

## Fix 1 — Parallel Playwright execution (`interaction_reviewer.py`)

**Problem (v2.8):** Interaction tests ran sequentially — one browser context
per test, one after the other. On a component-heavy frontend (5 components ×
4 interaction states = 20 tests) this added 60–120s of serial browser overhead
per job.

**Fix:** The embedded runner script (`INTERACTION_RUNNER_SCRIPT`) now uses
`asyncio.gather` to run all tests concurrently inside a single browser instance.
Each test gets its own context (isolated page, viewport, cookies) but all run
in parallel up to `MAX_CONCURRENCY = 8` simultaneous contexts, controlled by
an `asyncio.Semaphore` to avoid sandbox OOM.

Result ordering is preserved — `asyncio.gather` returns results in input order.

**Files changed:** `interaction_reviewer.py`

---

## Fix 2 — Max design regeneration cap + best-known fallback (`agent_loop.py`)

**Problem (v2.8):** `regenerate_design_spec()` could be called on every
iteration where visual review failed with a design-level issue. On a 5-iteration
job with persistent design failures, this meant 3 uncapped regeneration calls,
each costing an LLM round-trip. No fallback existed if all regenerations failed.

**Fix:** Two variables added before the iteration loop:

```python
design_regen_count = 0
MAX_DESIGN_REGENS  = 2
```

The regen block now branches:
- `design_regen_count < MAX_DESIGN_REGENS` → regenerate as before, increment counter
- cap reached → fall back to `select_design_spec_with_ab_test()` which picks the
  highest-pass-rate known spec for this task/stack. If no A/B spec exists, the
  current spec is kept and a warning is logged.

`previous_attempts` in the failure context now uses the accurate `design_regen_count`
instead of counting all visual failures in the journal (which overcounted).

**Files changed:** `agent_loop.py`

---

## Fix 3 — Double embed + double DB query in A/B tester (`design_ab_tester.py`)

**Problem (v2.8):** `select_design_spec_with_ab_test()` called `select_variant()`
which ran one embed + one DB query, then called `_load_variants()` again to find
the `spec_id` — a second embed + second DB query for the same task/stack. This
doubled the cost of every A/B spec lookup.

**Fix:** `select_variant()` now returns `(spec_dict, spec_id)` instead of just
`spec_dict`. `select_design_spec_with_ab_test()` is a thin wrapper that unpacks
the tuple — no second query, no second embed call.

The `_load_variants()` internal method is unchanged; callers that need the full
`DesignVariant` list (e.g. `record_result`) still use it directly.

**Files changed:** `design_ab_tester.py`

---

## Fix 4 — New components from regeneration in testid validator (`testid_validator.py`)

**Problem (v2.8):** When design regeneration added a brand-new component (e.g.
a `ToastNotification` not in the original spec), `validate_testids()` treated its
missing `data-testid` as a hard error. But no interaction test was ever generated
for that component — the interaction reviewer was seeded from the *original* spec.
The hard error was spurious and blocked the coder unnecessarily.

**Fix:** `validate_testids()` now accepts an optional `original_spec` parameter.
Components present in the regenerated spec but absent from the original are flagged
as `is_new_component: True` in the missing list.

Validation logic:
- `hard_missing` — pre-existing components missing their testids → blocks (`valid = False`)
- `soft_missing` — new components missing their testids → advisory only, included in
  suggestions but does not affect `valid`

Logging now emits `missing_hard` and `missing_soft` as separate counters.

**Files changed:** `testid_validator.py`

---

## Fix 5 — Scored design failure classifier (`design_regenerator.py`)

**Problem (v2.8):** `is_design_level_failure()` used a flat keyword list —
any match → design failure → regenerate. Hybrid failures (e.g. "button shadow
looks off", which matches no keyword and is silently treated as code-level) and
subtle design signals were misclassified. The binary approach had no tuning surface.

**Fix:** `is_design_level_failure(failure, threshold=0.5)` now accumulates a
weighted score:

| Signal tier       | Keywords (examples)                        | Weight |
|-------------------|--------------------------------------------|--------|
| Strong design     | z-index, clip, overflow, layout, stacking  | +0.40  |
| Medium design     | color, typography, spacing, responsive, flex | +0.25 |
| Weak design       | state, animation, transition, hover        | +0.15  |
| Code counter      | syntax, import, TypeError, module not found | −0.30 |

Score is clamped to [0.0, 1.0]. A failure is classified as design-level when
`score >= threshold`. The default threshold (0.5) matches previous behaviour for
clear design failures while correctly routing hybrid failures to code-level.

A companion `score_design_failure(failure) -> float` function is exported for
callers that want to log or apply a custom threshold.

**Files changed:** `design_regenerator.py`

---

## Migration instructions

No DB migration required. No new environment variables.

```bash
# Rebuild agent service
docker compose build agent_service

# Restart
docker compose up -d
```
