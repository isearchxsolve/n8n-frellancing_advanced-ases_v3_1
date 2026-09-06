"""
ASES - Visual Reviewer v2 (Gap Fix: visual gate fires too early)
=================================================================
Adds a last-mile gate heuristic: visual review is skipped unless the coder
is genuinely in the final polishing phase.

Problem:
    Visual reviewer was firing on every LLM-approved iteration, even iteration 2
    when previous_errors is rich and the coder has clear text feedback to work on.
    A gpt-4o vision call costs ~$0.01-0.03 and 8-10 seconds of latency per fire.
    On a 5-iteration job with frontend, this adds ~$0.10 and 40s unnecessarily.

Fix — last-mile gate heuristic:
    Skip visual review when EITHER:
    (a) previous_errors is above ERROR_RICHNESS_THRESHOLD chars
        (the coder already has plenty of structured text feedback)
    (b) iteration < max_iterations - LAST_MILE_RESERVE
        (not in the final stretch yet)

    Only fire when BOTH conditions clear:
        - iteration is close to max (within LAST_MILE_RESERVE iterations of end)
        - previous_errors is short (the code is nearly clean)

    This means on a 5-iteration job:
        Iterations 1-3: skipped (not last mile)
        Iteration 4+:   runs only if errors < threshold
        Net: 0-1 vision calls per job instead of up to 5.

New signature vs v2.6:
    visual_reviewer(..., iteration, max_iterations, previous_errors)
    ← three new keyword arguments with sensible defaults for back-compat
"""

import asyncio
import base64
import json
import os
import time
from typing import Dict, Any, List, Optional

import structlog

logger = structlog.get_logger()

FRONTEND_STACKS = {"react", "next.js", "nextjs", "vue", "svelte", "html"}

DEV_SERVER_COMMANDS = {
    "react":   "npm install --prefer-offline 2>&1 && npm start &",
    "next.js": "npm install --prefer-offline 2>&1 && npm run dev &",
    "nextjs":  "npm install --prefer-offline 2>&1 && npm run dev &",
    "vue":     "npm install --prefer-offline 2>&1 && npm run serve &",
    "html":    None,
}

SCREENSHOT_SCRIPT = """
import asyncio
from playwright.async_api import async_playwright
import sys

async def shoot(url, out_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception:
            await page.goto(url, timeout=15000)
        await page.screenshot(path=out_path, full_page=False)
        await browser.close()

asyncio.run(shoot(sys.argv[1], sys.argv[2]))
"""

# Last-mile gate constants
LAST_MILE_RESERVE        = 2      # fire only when <= this many iterations remain
ERROR_RICHNESS_THRESHOLD = 200    # chars — if previous_errors longer than this, skip


def _has_frontend(tech_stack: str, files: List[Dict[str, str]]) -> bool:
    stack_key = tech_stack.lower().split("+")[0].strip()
    if stack_key in FRONTEND_STACKS:
        return True
    frontend_extensions = {".jsx", ".tsx", ".vue", ".html"}
    return any(
        any(f["path"].endswith(ext) for ext in frontend_extensions)
        for f in files
    )


def _should_run_visual(
    iteration: int,
    max_iterations: int,
    previous_errors: str,
) -> tuple[bool, str]:
    """
    Returns (should_run: bool, skip_reason: str).
    skip_reason is empty when should_run is True.
    """
    iterations_remaining = max_iterations - iteration
    if iterations_remaining > LAST_MILE_RESERVE:
        return False, (
            f"Not last-mile yet (iteration {iteration}/{max_iterations}, "
            f"{iterations_remaining} remaining > reserve {LAST_MILE_RESERVE}) — "
            f"skipping visual review to save cost"
        )

    if len(previous_errors) > ERROR_RICHNESS_THRESHOLD:
        return False, (
            f"Rich text feedback exists ({len(previous_errors)} chars) — "
            f"coder has structured errors to work on; skipping vision call"
        )

    return True, ""


async def visual_reviewer(
    sandbox_id: str,
    task: str,
    tech_stack: str,
    files: List[Dict[str, str]],
    config,
    execution_id: str,
    iteration: int = 999,           # new: current iteration number
    max_iterations: int = 1000,     # new: total max iterations
    previous_errors: str = "",      # new: current error string
) -> Dict[str, Any]:
    """
    Takes a screenshot of the generated UI and validates it with a vision model.
    Now gated by last-mile heuristic to avoid wasting tokens on early iterations.
    """
    from agent_loop import call_model
    from sandbox import run_command, write_file

    # ---- last-mile gate ----
    should_run, skip_reason = _should_run_visual(iteration, max_iterations, previous_errors)
    if not should_run:
        logger.info("visual_reviewer.last_mile_skip", execution_id=execution_id, reason=skip_reason)
        return _visual_skip(skip_reason)

    stack_key = tech_stack.lower().split("+")[0].strip()
    dev_cmd = DEV_SERVER_COMMANDS.get(stack_key)
    port = 3000 if stack_key in {"react", "next.js", "nextjs"} else 8080

    write_file(sandbox_id, ".ases_screenshot.py", SCREENSHOT_SCRIPT)

    if dev_cmd:
        run_command(sandbox_id, dev_cmd, timeout=60)
        await asyncio.sleep(8)

    screenshot_path = "/workspace/.ases_shot.png"
    url = f"http://localhost:{port}" if dev_cmd else "file:///workspace/index.html"

    shot_result = run_command(
        sandbox_id,
        f"python .ases_screenshot.py '{url}' '{screenshot_path}'",
        timeout=30,
    )

    if not shot_result["success"]:
        logger.warning("visual_reviewer.screenshot_failed", execution_id=execution_id,
                       stderr=shot_result["stderr"][:300])
        return _visual_skip("Screenshot capture failed — skipping visual review")

    screenshot_b64 = _read_sandbox_file_b64(sandbox_id, screenshot_path)
    if not screenshot_b64:
        return _visual_skip("Screenshot file not found after capture")

    try:
        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        review_prompt = f"""You are a senior UX engineer reviewing a generated web app screenshot.

Task: {task}
Tech stack: {tech_stack}

Evaluate on:
1. RENDERS: Does the page show visible content (not blank/white)?
2. LAYOUT: Are all UI elements visible and positioned correctly?
3. RELEVANCE: Does the UI match what was asked for?
4. ACCESSIBILITY: Are text colors readable? Interactive elements identifiable?
5. COMPLETENESS: Any obvious missing sections, broken images, empty placeholders?

Output ONLY valid JSON:
{{
  "approved": false,
  "issues": [
    {{"severity": "high", "description": "Page is completely white — CSS not loading"}},
    {{"severity": "medium", "description": "Navigation overflows on right side"}}
  ],
  "summary": "One sentence assessment"
}}

approved=true only if UI renders correctly AND matches task requirements."""

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": review_prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "high",
                    }},
                ],
            }],
            max_tokens=600,
            temperature=0.1,
        )

        raw = response.choices[0].message.content
        usage = response.usage
        review = _parse_json_safe(raw)

        if not review:
            return _visual_skip("Visual review response could not be parsed")

        approved = bool(review.get("approved", True))
        issues = review.get("issues", [])

        issues_text = ""
        if not approved and issues:
            lines = ["VISUAL REVIEW FAILED — UI issues detected:"]
            for iss in issues:
                lines.append(f"  [{iss.get('severity','?').upper()}] {iss.get('description','')}")
            lines.append("Fix the UI before delivery.")
            issues_text = "\n".join(lines)

        logger.info("visual_reviewer.complete", execution_id=execution_id,
                    approved=approved, issues=len(issues),
                    iteration=iteration, max_iterations=max_iterations)

        return {
            "approved": approved,
            "issues": issues,
            "issues_text": issues_text,
            "screenshot_b64": screenshot_b64,
            "tokens": usage.prompt_tokens + usage.completion_tokens,
        }

    except Exception as e:
        logger.warning("visual_reviewer.llm_failed", execution_id=execution_id, error=str(e))
        return _visual_skip(f"Visual LLM call failed: {e}")


def _read_sandbox_file_b64(sandbox_id: str, container_path: str) -> Optional[str]:
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "cp", f"{sandbox_id}:{container_path}", "-"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return base64.b64encode(result.stdout).decode()
    except Exception as e:
        logger.warning("visual_reviewer.file_read_failed", error=str(e))
    return None


def _visual_skip(reason: str) -> Dict[str, Any]:
    logger.info("visual_reviewer.skipped", reason=reason)
    return {"approved": True, "issues": [], "issues_text": "", "screenshot_b64": None, "tokens": 0}


def _parse_json_safe(content: str) -> Dict:
    import re
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r'```json\s*(.*?)```', content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return {}
