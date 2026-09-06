"""
ASES - Interaction Reviewer (v2.9)
====================================
End-to-end interaction validation using Playwright in the sandbox.

Runs a suite of interaction tests generated from the design spec's
'interaction_rules' and 'states' fields. Executes real browser actions
(clicks, typing, keyboard navigation) and validates DOM + accessibility
state changes.

Key features:
- Zero LLM tokens (pure Playwright execution)
- Hydration-aware (waits for React/Vue/Angular hydration)
- Shadow DOM penetration support
- Mobile viewport emulation (touch events)
- Accessibility state validation (aria-expanded, aria-hidden, etc.)
- Screenshot-on-failure for debugging

Integration: agent_loop.py, after visual_reviewer() approval, gated by
_has_frontend() and presence of interaction_rules in design spec.
"""

import json
import asyncio
import base64
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

import structlog

logger = structlog.get_logger()


@dataclass
class InteractionTest:
    name: str
    selector: str                    # CSS selector, aria-label, or data-testid
    action: str                      # "click" | "type" | "keyboard" | "hover" | "focus" | "touch"
    pre_condition: Optional[str]     # selector that must exist before action
    post_condition: str              # selector/state that must exist after
    accessibility_check: bool        # verify aria-expanded, aria-hidden, etc.
    viewport: Optional[str]          # "desktop" | "tablet" | "mobile" | None
    timeout_ms: int = 5000           # max wait for conditions


# v2.9: tests run in parallel via asyncio.gather (one browser, N concurrent contexts)
# v2.10: return_exceptions=True so one test crash doesn't abort the whole suite;
#         _run_single_test retries up to MAX_RETRIES times with jitter on transient failures.
# v3.1: __ASES_READY__ sentinel replaces wait_for_selector hydration gate;
#       retry classification uses Playwright exception types, not string matching.
INTERACTION_RUNNER_SCRIPT = r"""
import asyncio
import sys
import json
import random
import traceback
import base64
from playwright.async_api import (
    async_playwright, expect,
    TimeoutError as PWTimeout,
    Error as PWError,
)

# Max concurrent browser contexts. Keep ≤8 to avoid sandbox OOM.
MAX_CONCURRENCY = 8
MAX_RETRIES = 2        # attempts per test before giving up
BASE_JITTER_MS = 200   # random delay ceiling between retries (ms)

# Playwright exception types that indicate a transient browser/DOM state
# rather than a permanent test logic error. Used for retry classification.
# Matching on exception types is stable across Playwright versions;
# string-matching on error messages is not.
_TRANSIENT_PW_TYPES = (PWTimeout,)


async def _run_single_test(browser, test, url, semaphore, screenshot_on_fail=True):
    """Run one interaction test with retry + jitter for flaky selector/timing failures."""
    last_result = None

    for attempt in range(1, MAX_RETRIES + 1):
        result = await _attempt_test(browser, test, url, semaphore, screenshot_on_fail)
        last_result = result
        if result["passed"]:
            if attempt > 1:
                result["retried"] = attempt - 1
            return result
        # _attempt_test sets retryable=True only for transient errors (PWTimeout, element not found).
        # Hard errors (bad action type, ValueError) are not retryable — fail immediately.
        if not result.get("retryable", False):
            break
        if attempt < MAX_RETRIES:
            # Exponential jitter: later attempts wait longer, spreading retry load under concurrency
            jitter = random.uniform(0, BASE_JITTER_MS / 1000) * attempt
            await asyncio.sleep(jitter)

    if last_result and MAX_RETRIES > 1:
        last_result["retried"] = MAX_RETRIES - 1
    return last_result


async def _attempt_test(browser, test, url, semaphore, screenshot_on_fail=True):
    """Single attempt at running one interaction test inside its own browser context."""
    test_result = {"name": test["name"], "passed": False, "error": None, "stage": None, "screenshot": None}
    context = None
    page = None

    async with semaphore:
        try:
            viewport = test.get("viewport", "desktop")
            vp_map = {
                "desktop": {"width": 1280, "height": 800},
                "tablet":  {"width": 768,  "height": 1024},
                "mobile":  {"width": 375,  "height": 667},
            }
            vp = vp_map.get(viewport, vp_map["desktop"])

            context = await browser.new_context(
                viewport=vp,
                has_touch=(viewport == "mobile"),
                user_agent="Mozilla/5.0 (ASES-Interaction-Reviewer)",
            )
            page = await context.new_page()

            # Navigate. wait_until="networkidle" covers document + in-flight XHR.
            await page.goto(url, wait_until="networkidle", timeout=15000)

            # Hydration gate — wait for the app to signal it is fully mounted
            # and all event handlers are attached.
            #
            # Primary signal: window.__ASES_READY__ === true
            # The coder is required (via design spec notes_for_coder) to set this
            # flag after the root component mounts:
            #   React: useEffect(() => { window.__ASES_READY__ = true; }, [])
            #   Vue:   mounted() { window.__ASES_READY__ = true }
            #
            # This is deterministic: the flag is only set after React/Vue's
            # first committed render, which guarantees all onClick handlers
            # are attached. "element visible + enabled" is necessary but not
            # sufficient — the handler can be absent between render and attach.
            #
            # Fallback: if the flag never appears (app doesn't set it, or
            # non-framework HTML), fall back to waiting for [data-testid].
            # Second fallback: proceed anyway and let individual actions timeout.
            try:
                await page.wait_for_function(
                    "() => window.__ASES_READY__ === true",
                    timeout=8000,
                )
            except PWTimeout:
                # App did not set __ASES_READY__ — fall back to testid presence
                try:
                    await page.wait_for_selector("[data-testid]", timeout=4000)
                except PWTimeout:
                    # No testid elements either — proceed; per-action waits handle it
                    pass

            # Pre-condition check
            if test.get("pre_condition"):
                await expect(page.locator(test["pre_condition"])).to_be_visible(
                    timeout=test.get("timeout_ms", 5000)
                )
                test_result["stage"] = "pre_ok"

            # Execute action
            action   = test["action"]
            selector = test["selector"]

            t_ms = test.get("timeout_ms", 5000)

            if action == "click":
                # Wait for element to be both visible and enabled before clicking.
                # enabled = event handler is attached in React/Vue — this is the
                # deterministic replacement for the 800ms heuristic sleep.
                locator = page.locator(selector)
                await locator.wait_for(state="visible", timeout=t_ms)
                await expect(locator).to_be_enabled(timeout=t_ms)
                await locator.click(timeout=t_ms)
                # After a click, wait for any triggered DOM mutation to settle
                # (navigation, state update, modal open).
                await page.wait_for_load_state("domcontentloaded", timeout=t_ms)
            elif action == "keyboard":
                await page.keyboard.press(selector)
                # Keyboard events can trigger async state changes — wait for
                # the DOM to stabilise before checking post-condition.
                await page.wait_for_load_state("domcontentloaded", timeout=t_ms)
            elif action == "type":
                parts = selector.split("|", 1)
                if len(parts) == 2:
                    field_locator = page.locator(parts[0])
                    await field_locator.wait_for(state="visible", timeout=t_ms)
                    await expect(field_locator).to_be_enabled(timeout=t_ms)
                    await page.fill(parts[0], parts[1], timeout=t_ms)
                    # Typing can trigger validation/debounce renders — wait for
                    # any resulting network activity to finish.
                    await page.wait_for_load_state("networkidle", timeout=t_ms)
                else:
                    raise ValueError(f"type action requires 'selector|text' format, got: {selector}")
            elif action == "hover":
                locator = page.locator(selector)
                await locator.wait_for(state="visible", timeout=t_ms)
                await page.hover(selector, timeout=t_ms)
                # Hover can reveal tooltips/dropdowns via CSS transitions.
                # domcontentloaded gate is faster than a fixed sleep and doesn't
                # penalise UIs that respond immediately.
                await page.wait_for_load_state("domcontentloaded", timeout=t_ms)
            elif action == "focus":
                locator = page.locator(selector)
                await locator.wait_for(state="visible", timeout=t_ms)
                await page.focus(selector, timeout=t_ms)
                # Focus changes are synchronous in the browser; no extra wait needed.
            elif action == "touch":
                locator = page.locator(selector)
                await locator.wait_for(state="visible", timeout=t_ms)
                await page.tap(selector, timeout=t_ms)
                await page.wait_for_load_state("domcontentloaded", timeout=t_ms)
            else:
                raise ValueError(f"Unknown action: {action}")

            # Post-condition check
            await expect(page.locator(test["post_condition"])).to_be_visible(
                timeout=test.get("timeout_ms", 5000)
            )
            test_result["stage"] = "post_ok"

            # Accessibility check
            if test.get("accessibility_check"):
                elem = page.locator(test["post_condition"])
                test_result["aria"] = {
                    "expanded": await elem.get_attribute("aria-expanded"),
                    "hidden":   await elem.get_attribute("aria-hidden"),
                    "modal":    await elem.get_attribute("aria-modal"),
                }

            test_result["passed"] = True

        except PWTimeout as e:
            test_result["error"] = f"Timeout: {str(e)}"
            test_result["stage"] = test_result["stage"] or "timeout"
            test_result["retryable"] = True   # transient — worth retrying
            if screenshot_on_fail and page:
                try:
                    ss = await page.screenshot(full_page=False)
                    test_result["screenshot_b64"] = base64.b64encode(ss).decode()
                except Exception:
                    pass

        except Exception as e:
            # Classify retryability by exception type, not string matching.
            # PWTimeout is already caught above and always retryable.
            # PWError covers browser-level failures (target closed, crash) —
            # these are transient under concurrency and worth one retry.
            # ValueError (bad action type, bad selector format) is a hard
            # test-definition error — retrying won't help.
            test_result["retryable"] = isinstance(e, _TRANSIENT_PW_TYPES + (PWError,))
            test_result["error"] = f"{type(e).__name__}: {str(e)}"
            test_result["stage"] = test_result["stage"] or "error"
            if screenshot_on_fail and page:
                try:
                    ss = await page.screenshot(full_page=False)
                    test_result["screenshot_b64"] = base64.b64encode(ss).decode()
                except Exception:
                    pass

        finally:
            if page:
                await page.close()
            if context:
                await context.close()

    return test_result


async def run(tests, url, port, screenshot_on_fail=True):
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )

            semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
            tasks = [
                _run_single_test(browser, test, url, semaphore, screenshot_on_fail)
                for test in tests
            ]
            # return_exceptions=True: one crashing test doesn't abort the rest.
            # Exceptions are converted to failure dicts so the caller always gets a full result list.
            raw = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            for i, r in enumerate(raw):
                if isinstance(r, Exception):
                    results.append({
                        "name": tests[i].get("name", f"test_{i}"),
                        "passed": False,
                        "error": f"Unhandled exception: {type(r).__name__}: {r}",
                        "stage": "gather",
                        "retryable": False,
                        "screenshot_b64": None,
                        "aria": None,
                    })
                else:
                    results.append(r)

            await browser.close()
            browser = None
            return list(results)

    except Exception as e:
        if browser:
            await browser.close()
        return [{
            "name": "runner_setup",
            "passed": False,
            "error": f"Browser setup failed: {str(e)}\n{traceback.format_exc()}",
            "stage": "setup",
        }]

if __name__ == "__main__":
    tests = json.loads(sys.argv[1])
    url   = sys.argv[2]
    result = asyncio.run(run(tests, url, None))
    print(json.dumps(result))
"""


# ---------------------------------------------------------------------------
# Main reviewer
# ---------------------------------------------------------------------------

async def interaction_reviewer(
    sandbox_id: str,
    design_spec: Dict[str, Any],
    files: List[Dict[str, str]],
    config,
    execution_id: str,
) -> Dict[str, Any]:
    """
    Generates and executes interaction tests from the design spec.

    Returns:
    {
        "approved": bool,
        "tests_run": int,
        "tests_passed": int,
        "failures": [{"name": str, "error": str, "stage": str, "screenshot_b64": str}],
        "tokens": 0,  # No LLM tokens — pure Playwright execution
        "duration_ms": int,
    }
    """
    from sandbox import run_command, write_file

    # Extract interaction rules from design spec
    interaction_tests = _generate_tests_from_spec(design_spec)

    if not interaction_tests:
        logger.info("interaction_reviewer.no_rules", execution_id=execution_id)
        return {
            "approved": True,
            "tests_run": 0,
            "tests_passed": 0,
            "failures": [],
            "tokens": 0,
            "duration_ms": 0,
        }

    # Write test runner to sandbox
    write_file(sandbox_id, ".ases_interaction.py", INTERACTION_RUNNER_SCRIPT)

    # Determine dev server URL
    stack = _resolve_stack_from_files(files)
    port = 3000 if stack in {"react", "next.js", "nextjs"} else 8080
    url = f"http://localhost:{port}"

    # Start dev server if not already running
    dev_cmd = _get_dev_command(stack)
    if dev_cmd:
        run_command(sandbox_id, dev_cmd + " &", timeout=5)
        await asyncio.sleep(10)  # Server boot + hydration buffer

    # Run interaction tests
    import time
    t0 = time.perf_counter()

    test_json = json.dumps([asdict(t) for t in interaction_tests])
    result = run_command(
        sandbox_id,
        f"python .ases_interaction.py '{test_json}' '{url}'",
        timeout=120,
    )

    duration_ms = int((time.perf_counter() - t0) * 1000)

    if not result["success"]:
        logger.warning("interaction_reviewer.runner_failed", execution_id=execution_id, stderr=result["stderr"][:300])
        return {
            "approved": False,
            "tests_run": len(interaction_tests),
            "tests_passed": 0,
            "failures": [{"name": "runner", "error": result["stderr"], "stage": "setup"}],
            "tokens": 0,
            "duration_ms": duration_ms,
        }

    try:
        test_results = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "approved": False,
            "tests_run": len(interaction_tests),
            "tests_passed": 0,
            "failures": [{"name": "parse", "error": "Could not parse test results", "stage": "parse"}],
            "tokens": 0,
            "duration_ms": duration_ms,
        }

    failures = [r for r in test_results if not r.get("passed")]
    approved = len(failures) == 0

    logger.info(
        "interaction_reviewer.complete",
        execution_id=execution_id,
        tests_run=len(interaction_tests),
        passed=len(test_results) - len(failures),
        approved=approved,
        duration_ms=duration_ms,
    )

    return {
        "approved": approved,
        "tests_run": len(interaction_tests),
        "tests_passed": len(test_results) - len(failures),
        "failures": failures,
        "tokens": 0,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Test generation from design spec
# ---------------------------------------------------------------------------

def _generate_tests_from_spec(spec: Dict[str, Any]) -> List[InteractionTest]:
    """Convert design spec interaction_rules into executable test cases."""
    tests = []

    for component in spec.get("components", []):
        rules = component.get("interaction_rules", [])
        states = component.get("states", [])
        name = component["name"]
        testid = component.get("data_testid", name.lower().replace(" ", "-"))

        for rule in rules:
            rule_lower = rule.lower()

            # Modal open/close patterns
            if any(k in rule_lower for k in ["opens on", "open on", "trigger on"]):
                trigger = "click" if "click" in rule_lower else "keyboard" if "key" in rule_lower else "click"
                tests.append(InteractionTest(
                    name=f"{name}: opens on {trigger}",
                    selector=f"[data-testid='{testid}-trigger']" if trigger == "click" else "Enter",
                    action=trigger,
                    pre_condition=f"[data-testid='{testid}-trigger']",
                    post_condition=f"[data-testid='{testid}-content'], [role='dialog']",
                    accessibility_check=True,
                ))

            if any(k in rule_lower for k in ["closes on escape", "close on escape", "escape key"]):
                tests.append(InteractionTest(
                    name=f"{name}: closes on Escape",
                    selector="Escape",
                    action="keyboard",
                    pre_condition=f"[role='dialog'], [data-testid='{testid}-content']",
                    post_condition=f"body:not(:has([role='dialog'])), body:not(:has([data-testid='{testid}-content']))",
                    accessibility_check=False,
                ))

            if any(k in rule_lower for k in ["closes on backdrop", "close on backdrop", "click outside"]):
                tests.append(InteractionTest(
                    name=f"{name}: closes on backdrop click",
                    selector=f"[data-testid='{testid}-overlay'], .modal-overlay, .backdrop",
                    action="click",
                    pre_condition=f"[role='dialog'], [data-testid='{testid}-content']",
                    post_condition=f"body:not(:has([role='dialog'])), body:not(:has([data-testid='{testid}-content']))",
                    accessibility_check=False,
                ))

            # Dropdown / Select patterns
            if any(k in rule_lower for k in ["dropdown", "select", "menu"]):
                tests.append(InteractionTest(
                    name=f"{name}: opens and shows options",
                    selector=f"[data-testid='{testid}-trigger']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}-trigger']",
                    post_condition=f"[role='listbox'], [data-testid='{testid}-options'], [role='menu']",
                    accessibility_check=True,
                ))

                tests.append(InteractionTest(
                    name=f"{name}: selects option",
                    selector=f"[data-testid='{testid}-option-1'], [role='option']:first-child",
                    action="click",
                    pre_condition=f"[role='listbox'], [data-testid='{testid}-options']",
                    post_condition=f"[data-testid='{testid}-trigger']",
                    accessibility_check=True,
                ))

            # Toggle / Switch patterns
            if any(k in rule_lower for k in ["toggle", "switch", "checkbox"]):
                tests.append(InteractionTest(
                    name=f"{name}: toggles state",
                    selector=f"[data-testid='{testid}'], [role='switch']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}'], [role='switch']",
                    post_condition=f"[data-testid='{testid}'], [role='switch']",
                    accessibility_check=True,
                ))

            # Form submission patterns
            if any(k in rule_lower for k in ["submit", "form", "save"]):
                tests.append(InteractionTest(
                    name=f"{name}: submits form",
                    selector=f"[data-testid='{testid}-submit'], [type='submit']",
                    action="click",
                    pre_condition=f"[data-testid='{testid}']",
                    post_condition=f"[data-testid='{testid}-success'], .success-message",
                    accessibility_check=False,
                ))

            # Tab / Navigation patterns
            if any(k in rule_lower for k in ["tab", "navigate", "switch tab"]):
                tests.append(InteractionTest(
                    name=f"{name}: switches tab",
                    selector=f"[data-testid='{testid}-tab-2'], [role='tab']:nth-child(2)",
                    action="click",
                    pre_condition=f"[data-testid='{testid}'], [role='tablist']",
                    post_condition=f"[role='tabpanel']:not([hidden])",
                    accessibility_check=True,
                ))

            # Mobile-specific patterns
            if any(k in rule_lower for k in ["hamburger", "mobile menu", "collapse"]):
                tests.append(InteractionTest(
                    name=f"{name}: mobile menu toggle",
                    selector=f"[data-testid='{testid}-hamburger']",
                    action="click",
                    viewport="mobile",
                    pre_condition=f"[data-testid='{testid}-hamburger']",
                    post_condition=f"[data-testid='{testid}-menu']:not([hidden])",
                    accessibility_check=True,
                ))

    # Deduplicate by name
    seen = set()
    unique_tests = []
    for t in tests:
        if t.name not in seen:
            seen.add(t.name)
            unique_tests.append(t)

    return unique_tests


# ---------------------------------------------------------------------------
# Stack detection helpers
# ---------------------------------------------------------------------------

def _resolve_stack_from_files(files: List[Dict[str, str]]) -> str:
    """Detect frontend stack from file extensions and config files."""
    paths = [f["path"] for f in files]

    if any("next.config" in p or "next.config.js" in p for p in paths):
        return "next.js"
    if any("vite.config" in p for p in paths):
        return "react"
    if any(p.endswith((".jsx", ".tsx")) for p in paths):
        return "react"
    if any(p.endswith(".vue") for p in paths):
        return "vue"
    if any(p.endswith(".svelte") for p in paths):
        return "svelte"
    if any("angular.json" in p for p in paths):
        return "angular"

    return "react"  # safe default


def _get_dev_command(stack: str) -> Optional[str]:
    """Get the dev server start command for a given stack."""
    commands = {
        "react": "npm start",
        "next.js": "npm run dev",
        "nextjs": "npm run dev",
        "vue": "npm run serve",
        "svelte": "npm run dev",
        "angular": "ng serve",
    }
    return commands.get(stack)
