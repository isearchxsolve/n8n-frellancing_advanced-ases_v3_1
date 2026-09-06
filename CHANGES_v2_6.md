# ASES v2.6 — Gap Fixes Patch

## What changed

This release integrates the five architectural gap fixes into `_dev_pipeline()` in `agent_loop.py`, and adds five new modules to `agent_service/`.

### New modules added to `agent_service/`

| File | Fix | Purpose |
|------|-----|---------|
| `iteration_journal.py` | Fix 1 | Cross-iteration architectural memory — extracts KEEP constraints from passing iterations |
| `semantic_differ.py` | Fix 2 | AST-level regression detection — catches broken imports before tests run |
| `clarifier_agent.py` | Fix 3 | Pre-flight requirement scoring — blocks or augments underspecified tasks |
| `visual_reviewer.py` | Fix 4 | UI/UX visual validation via Playwright screenshot + vision model |
| `dependency_debugger.py` | Fix 5 | Multi-file error attribution — enriches stack traces with import graph context |

### SQL migration

`database/migration_gap_fixes.sql` — run against your Postgres instance to add the tables required by Fix 1 (`iteration_journal`) and Fix 3 (`clarifier_cache`).

### `agent_loop.py` changes

`_dev_pipeline()` replaced wholesale with the patched version. All other functions unchanged.

Key behaviour changes:
- Fix 3 (clarifier) now runs **before** billing preflight, so underspecified tasks are rejected before any tokens are spent.
- Fix 1 (journal) records every iteration outcome and injects `=== ARCHITECTURAL JOURNAL ===` KEEP blocks into coder requirements from iteration 2 onward.
- Fix 2 (differ) runs after each coder output and annotates the error feedback with broken import warnings when relevant.
- Fix 4 (visual) runs as the final gate, only for frontend stacks, only after LLM review approves.
- Fix 5 (debugger) enriches raw test stderr with a `=== DEPENDENCY ANALYSIS ===` block on every test failure.
- API response now includes `clarity_score` and `clarity_assumptions` fields.

### No breaking changes to the external API surface.
