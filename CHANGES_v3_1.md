# ASES v3.1 — Deterministic Hydration Gate + Typed Retry Classification

Closes the two remaining real-world behaviour risks flagged in the v3.0 audit.
No new dependencies. No DB migration required.

---

## Fix 1 — Deterministic UI readiness via `__ASES_READY__` sentinel
**Files:** `interaction_reviewer.py`, `design_agent.py`

**Problem (v3.0):**
The hydration gate `wait_for_selector("[data-testid]")` tells us an element is in
the DOM, but not that its event handlers are attached. In React and Vue, the
render-to-handler pipeline has two distinct moments:

1. React commits the DOM node (selector becomes findable)
2. React attaches `onClick` and other handlers (element becomes interactive)

Between these two moments — typically a few milliseconds, but up to ~50ms under
CPU load — a click lands on an element whose handler isn't yet registered. The
action succeeds at the Playwright level (element was visible and enabled) but
nothing happens in the app. The post-condition check then times out with a
cryptic "element not visible" error that looks like a test bug.

`to_be_enabled()` does not close this gap. The DOM `disabled` attribute reflects
HTML state, not whether React's synthetic event system has wired up the handler.

**Fix — two-layer approach:**

**Layer 1 — `window.__ASES_READY__` sentinel (app instrumentation)**

The coder is now required to set a flag after the root component's first committed
render, which in React is guaranteed to happen after all handlers are attached:

```jsx
// React
useEffect(() => { window.__ASES_READY__ = true; }, [])

// Vue
mounted() { window.__ASES_READY__ = true }

// Vanilla JS
document.addEventListener('DOMContentLoaded', () => { window.__ASES_READY__ = true })
```

The instruction is injected into the coder prompt via two paths:
- `design_agent.py` — added to `spec["notes_for_coder"]` during spec validation,
  and always appended in `format_design_for_coder()` even for pre-v3.1 specs
- The instruction includes all three framework variants so the coder can pick the
  right one regardless of stack

**Layer 2 — `wait_for_function` with `[data-testid]` fallback**

The hydration gate in `INTERACTION_RUNNER_SCRIPT` is now:

```python
# Primary: deterministic — waits until app signals full mount
await page.wait_for_function(
    "() => window.__ASES_READY__ === true",
    timeout=8000,
)
# Fallback: if app doesn't set the flag (legacy specs, pre-v3.1 jobs)
await page.wait_for_selector("[data-testid]", timeout=4000)
# Second fallback: proceed anyway; per-action waits handle individual elements
```

The 8-second timeout on `wait_for_function` is deliberately generous: `networkidle`
already fired, so this only waits for the JS microtask that sets the flag. In
practice it completes in <50ms on a healthy app. If it times out, the test
continues rather than aborting — the fallback path ensures backward compatibility
with any apps that don't set the sentinel.

**Why this is deterministic:** `useEffect` with an empty dependency array fires
synchronously after React's commit phase, which is the same phase that attaches
all synthetic event listeners. Setting `__ASES_READY__` inside it is therefore
a precise post-handler-attachment signal, not a timing heuristic.

---

## Fix 2 — Typed retry classification
**File:** `interaction_reviewer.py`

**Problem (v3.0):**
The `except Exception` handler classified retryability by string-matching on the
error message:

```python
test_result["retryable"] = "not found" in err_str or "element" in err_str
```

This has two failure modes:
- **False positives:** A `ValueError("Unknown action: element-click")` — a hard
  test-definition error — would match on `"element"` and be retried pointlessly.
- **False negatives:** A browser-level `playwright.async_api.Error` (target page
  closed, browser crash under concurrency) would not match and would be treated
  as permanent, discarding a result that a retry would have recovered.

Playwright's exception hierarchy is stable across minor versions:
- `TimeoutError` — selector/condition timed out (already caught above, always retryable)
- `Error` — base class for browser/protocol-level failures (transient under load)
- `ValueError` — test definition error (hard, non-retryable)

**Fix:**

```python
from playwright.async_api import Error as PWError

_TRANSIENT_PW_TYPES = (PWTimeout,)   # extend if needed

# In except Exception handler:
test_result["retryable"] = isinstance(e, _TRANSIENT_PW_TYPES + (PWError,))
```

`PWError` is imported alongside `PWTimeout` at the top of the script. The
`_TRANSIENT_PW_TYPES` tuple is defined as a named constant so it can be extended
without touching the handler logic.

`ValueError` is now correctly classified as non-retryable (it won't match either
type), while `PWError` is correctly classified as retryable.

---

## Migration instructions

No DB migration. No new environment variables. No new dependencies.

```bash
docker compose build agent_service
docker compose up -d
```

**Existing jobs in flight:** The sentinel is injected at code-generation time. Jobs
that started before the upgrade will not have `window.__ASES_READY__` in their
generated code. The fallback path (`wait_for_selector("[data-testid]")`) ensures
these jobs continue to work exactly as they did in v3.0.
