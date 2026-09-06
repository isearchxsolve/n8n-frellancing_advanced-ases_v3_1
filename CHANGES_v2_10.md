# ASES v2.10 — Three Risk Mitigations from v2.9 Audit

Closes the three remaining risks flagged in the post-v2.9 external review,
plus one internal code-quality fix identified during audit of the actual source.

No external API changes. No new dependencies. No DB migration required.

---

## Fix 1 — Playwright gather crash isolation + retry with jitter (`interaction_reviewer.py`)

**Problem (v2.9):**
`asyncio.gather` was called with `return_exceptions=False` (the default). A single
test throwing an unhandled exception would propagate immediately, cancelling all
remaining in-flight test coroutines and returning no results for them. Additionally,
transient timing failures (selector not yet in DOM, hydration lag) had no retry path
— a flaky result was a permanent failure.

**Fix — two changes inside `INTERACTION_RUNNER_SCRIPT`:**

1. `return_exceptions=True` on `asyncio.gather`. Exceptions from individual tests
   are caught in the results list and converted to failure dicts with
   `stage: "gather"`. All other tests complete normally.

2. `_run_single_test` is now a retry wrapper (up to `MAX_RETRIES = 2` attempts).
   Between attempts it sleeps a random jitter of 0–`BASE_JITTER_MS` ms (200 ms
   ceiling). Retry is only triggered for `Timeout` and `not found` errors —
   hard errors like unknown action type fail immediately without retry.
   Retried results include a `retried` counter for observability.

The actual test logic is unchanged, extracted into `_attempt_test`.

**Files changed:** `interaction_reviewer.py`

---

## Fix 2 — Similarity-weighted fallback spec selection (`design_ab_tester.py`, `agent_loop.py`)

**Problem (v2.9):**
When the regen cap was reached, `agent_loop.py` called `select_design_spec_with_ab_test`,
which uses epsilon-greedy bandit selection optimised for exploration/exploitation balance.
At cap-reached time, exploration is wasteful — you want the single best known spec. But
"best" was implicitly pass_rate-only, which could surface a high-performing spec that was
generated for a different task context (low similarity), producing layout mismatch.

**Fix:**
New function `select_best_fallback_spec` in `design_ab_tester.py` selects by blended score:

```
blended = similarity * 0.7 + pass_rate * 0.3
```

This ensures the fallback spec is both contextually relevant to the current task/stack AND
historically reliable. The 70/30 split is conservative — similarity dominates because a
contextually wrong spec will fail even with a perfect pass_rate history.

`agent_loop.py` cap-reached branch now calls `select_best_fallback_spec` instead of
`select_design_spec_with_ab_test`. Selection decision is logged with `similarity`,
`pass_rate`, and `blended` fields for future threshold tuning.

**Files changed:** `design_ab_tester.py`, `agent_loop.py`

---

## Fix 3 — Per-tenant configurable failure classifier threshold (`models.py`, `agent_loop.py`)

**Problem (v2.9):**
The `is_design_level_failure` threshold of 0.5 was hardcoded. Tenants with different
frontend complexity profiles (e.g. heavy animation UIs vs simple form builders) will
have systematically different failure distributions. A fixed threshold means some
tenants over-regenerate (threshold too low) and others under-regenerate (too high).

**Fix:**
`TenantConfig` gains a new field:

```python
design_failure_threshold: float = 0.5
```

Default is 0.5, preserving existing behaviour. `agent_loop.py` passes this through:

```python
is_design_level_failure(i, threshold=config.design_failure_threshold)
```

Tenants can override via their config row in the DB or per-request payload.
No migration required — the field defaults cleanly for all existing tenants.

**Files changed:** `models.py`, `agent_loop.py`

---

## Fix 4 — Scorer deduplication (`design_regenerator.py`)

**Problem (v2.9, found during source audit — not in external review):**
`is_design_level_failure` and `score_design_failure` were two separate functions
containing identical keyword lists and scoring logic copy-pasted verbatim. Any
tuning to one would silently diverge from the other.

**Fix:**
`score_design_failure` is now the single implementation. `is_design_level_failure`
is a one-liner that delegates to it:

```python
def is_design_level_failure(failure, threshold=0.5) -> bool:
    return score_design_failure(failure) >= threshold
```

All keyword weights and scoring logic live in exactly one place.

**Files changed:** `design_regenerator.py`

---

## Migration instructions

No DB migration. No new environment variables.

```bash
docker compose build agent_service
docker compose up -d
```

To adjust the failure classifier threshold for a tenant, update their config:

```python
config = TenantConfig(
    ...,
    design_failure_threshold=0.4,  # more sensitive — regen earlier
)
```
