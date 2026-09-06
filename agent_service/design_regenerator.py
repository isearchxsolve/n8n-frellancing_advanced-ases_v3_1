"""
ASES - Design Regenerator (v3.0)
==================================
Regenerates design specifications when visual or interaction review fails,
incorporating failure feedback to produce a corrected spec.

Problem in v2.6:
    When visual review fails ("modal is clipped by parent"), the error goes
    to the coder, not the designer. The design spec remains unchanged across
    iterations, meaning the coder is trying to fix a design-level problem
    with code-level changes.

Solution:
    1. Detect when a failure is design-level (not code-level)
    2. Feed failure context back to design_agent with "regenerate" mode
    3. Design agent produces a corrected spec (e.g., higher z-index, different layout)
    4. Coder receives updated spec on next iteration

v3.0 additions:
    patch_design_spec — surgical patch pass that runs BEFORE full regeneration.
    Targets only the failing component/property rather than rewriting the whole
    spec. ~10x cheaper (300 tokens vs 2500) and ~5x faster (1 LLM call, no
    schema reconstruction). Falls back to full regeneration if the patch produces
    invalid JSON or doesn't resolve the flagged fields.

    Call order in agent_loop:
        1. patch_design_spec()     ← new, cheap, targeted
        2. regenerate_design_spec() ← existing, expensive, full rewrite (fallback)

Integration: agent_loop.py — called when visual/interaction fails and
journal indicates this is a recurring design issue.
"""

import json
from typing import Dict, Any, List, Optional

import structlog

from agent_loop import call_model

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# v3.0: Surgical patch pass (runs before full regen)
# ---------------------------------------------------------------------------

async def patch_design_spec(
    original_spec: Dict[str, Any],
    failure_context: Dict[str, Any],
    config,
    execution_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Attempt a targeted, property-level fix to a failing design spec.

    Unlike regenerate_design_spec, this does NOT rewrite the whole spec.
    It asks the model to output ONLY the delta — a minimal JSON fragment
    describing which fields to overwrite — then merges it onto the original.

    Returns the patched spec dict on success, or None if the patch is
    invalid/insufficient (caller should fall through to full regen).

    Cost: ~300 tokens (vs ~2500 for full regen).
    Latency: ~1s (vs ~5-8s for full regen).

    Patch schema the model must return:
    {
      "target": "component" | "design_system" | "layout",
      "component_name": "<name if target==component>",
      "patches": {
        "<dot.path.key>": <new_value>,
        ...
      },
      "rationale": "<one-line explanation>"
    }

    dot.path.key examples:
      "layout_rules[0]"              → component.layout_rules[0]
      "design_system.colors.primary" → spec.design_system.colors.primary
      "states[1]"                    → component.states[1]
    """
    issues = failure_context.get("issues", [])
    if not issues:
        return None

    # Identify which components are implicated — pass only their slice to the model
    implicated = {i.get("component", "").lower() for i in issues if i.get("component")}
    relevant_components = [
        c for c in original_spec.get("components", [])
        if c.get("name", "").lower() in implicated or not implicated
    ][:3]  # cap at 3 to keep prompt small

    # Build a compact spec excerpt: design_system + implicated components only
    spec_excerpt = {
        "design_system": original_spec.get("design_system", {}),
        "layout": original_spec.get("layout", {}),
        "components": relevant_components,
    }

    system_prompt = """\
You are a design spec surgeon. You receive a PARTIAL design spec and a list of failures.
Your job: output the MINIMUM change needed to fix the failures.

RULES:
1. Output ONLY valid JSON matching this schema — no prose, no markdown:
{
  "target": "component" | "design_system" | "layout",
  "component_name": "<exact component name if target==component, else omit>",
  "patches": {
    "<field_name or nested.path>": <new_value>
  },
  "rationale": "<one sentence>"
}
2. Use dot notation for nested fields: "design_system.colors.primary" = "#FF0000"
3. For array fields, use index notation: "layout_rules[0]" = "position: fixed"
4. Patch ONLY what is broken — do not change unrelated fields.
5. Preserve all data_testid values exactly.
6. If you cannot fix it with a targeted patch, output: {"target": "CANNOT_PATCH"}"""

    failure_lines = "\n".join(
        f"  [{i.get('severity','?').upper()}] {i.get('description','')} "
        f"(component: {i.get('component','unknown')})"
        for i in issues
    )
    user_prompt = f"""\
Failure type: {failure_context.get('type', 'visual')}
Iteration: {failure_context.get('iteration', '?')}

Failures:
{failure_lines}

Relevant spec excerpt:
{json.dumps(spec_excerpt, indent=2)}

Output the patch JSON now."""

    content, inp_tok, out_tok = await call_model(
        model=config.reviewer_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,   # deterministic — we want the minimal correct fix
        max_tokens=400,
        execution_id=execution_id,
        call_type="reviewer",
    )

    patch = _parse_design_json(content)
    if not patch:
        logger.warning("design_patch.parse_failed", execution_id=execution_id)
        return None

    if patch.get("target") == "CANNOT_PATCH":
        logger.info("design_patch.cannot_patch", execution_id=execution_id,
                    rationale=patch.get("rationale", ""))
        return None

    # Apply the patch onto a deep copy of the original spec
    import copy
    patched = copy.deepcopy(original_spec)
    patches = patch.get("patches", {})

    if not patches:
        logger.warning("design_patch.empty_patches", execution_id=execution_id)
        return None

    try:
        target = patch.get("target", "")
        comp_name = patch.get("component_name", "")

        if target == "component" and comp_name:
            # Find the target component and apply patches to it
            target_comp = next(
                (c for c in patched.get("components", [])
                 if c.get("name", "").lower() == comp_name.lower()),
                None,
            )
            if target_comp is None:
                logger.warning("design_patch.component_not_found",
                               execution_id=execution_id, name=comp_name)
                return None
            _apply_patches(target_comp, patches)

        elif target == "design_system":
            _apply_patches(patched.setdefault("design_system", {}), patches)

        elif target == "layout":
            _apply_patches(patched.setdefault("layout", {}), patches)

        else:
            # Generic: apply top-level patches directly to spec root
            _apply_patches(patched, patches)

    except Exception as e:
        logger.warning("design_patch.apply_failed", execution_id=execution_id, error=str(e))
        return None

    logger.info(
        "design_patch.applied",
        execution_id=execution_id,
        target=target,
        component=comp_name or "(root)",
        fields_patched=len(patches),
        tokens=inp_tok + out_tok,
        rationale=patch.get("rationale", ""),
    )

    return patched


def _apply_patches(obj: Dict[str, Any], patches: Dict[str, Any]) -> None:
    """
    Apply a flat dict of dot-notation patches to a mutable dict in-place.

    Supported path forms:
      "key"              → obj["key"] = value
      "nested.key"       → obj["nested"]["key"] = value
      "array_key[0]"     → obj["array_key"][0] = value
      "nested.arr[2]"    → obj["nested"]["arr"][2] = value
    """
    import re as _re

    for path, value in patches.items():
        parts = _re.split(r'\.(?![^\[]*\])', path)  # split on dots not inside []
        target = obj
        for part in parts[:-1]:
            idx_match = _re.match(r'^(\w+)\[(\d+)\]$', part)
            if idx_match:
                key, idx = idx_match.group(1), int(idx_match.group(2))
                target = target.setdefault(key, [])[idx]
            else:
                target = target.setdefault(part, {})

        last = parts[-1]
        idx_match = _re.match(r'^(\w+)\[(\d+)\]$', last)
        if idx_match:
            key, idx = idx_match.group(1), int(idx_match.group(2))
            arr = target.setdefault(key, [])
            if idx < len(arr):
                arr[idx] = value
            else:
                arr.append(value)
        else:
            target[last] = value


async def regenerate_design_spec(
    original_spec: Dict[str, Any],
    failure_context: Dict[str, Any],
    task: str,
    tech_stack: str,
    requirements: str,
    config,
    execution_id: str,
) -> Dict[str, Any]:
    """
    Regenerate a design spec incorporating failure feedback.

    Args:
        original_spec: The design spec that failed
        failure_context: {
            "type": "visual" | "interaction",
            "issues": [{"severity": str, "description": str, "component": str}],
            "iteration": int,
            "previous_attempts": int,
        }

    Returns:
        New design_result dict (same format as design_agent output)
    """

    system_prompt = """You are a senior product designer fixing a design specification that failed review.

You will receive:
1. The original design spec (JSON)
2. A list of failures from visual review or interaction testing
3. The number of previous attempts

RULES:
1. Output ONLY valid JSON matching the original schema.
2. Fix the SPECIFIC issues mentioned — do not change unrelated parts.
3. If z-index is too low, increase it explicitly.
4. If a component is clipped, add overflow or positioning rules.
5. If interaction tests fail, add missing states or clarify interaction_rules.
6. If this is the 2nd+ regeneration, make more aggressive changes.
7. Preserve the data_testid values — they must remain stable.

Output format: Same as design_agent.py DESIGN_SCHEMA_DOC."""

    user_prompt = f"""Task: {task}
Tech Stack: {tech_stack}
Requirements: {requirements or "(none)"}

Original Design Spec:
{json.dumps(original_spec, indent=2)}

Failure Context:
Type: {failure_context['type']}
Iteration: {failure_context['iteration']}
Previous Attempts: {failure_context.get('previous_attempts', 0)}

Issues:
{chr(10).join(f"  - [{i.get('severity', '?')}] {i.get('description', '')} (component: {i.get('component', 'unknown')})" for i in failure_context.get('issues', []))}

Generate a CORRECTED design specification that addresses these failures."""

    content, inp_tok, out_tok = await call_model(
        model=config.reviewer_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,  # Slightly higher for creative fixes
        max_tokens=2500,
        execution_id=execution_id,
        call_type="reviewer",
    )

    spec = _parse_design_json(content)

    if not spec:
        logger.warning("design_regenerator.parse_failed", execution_id=execution_id)
        return {
            "has_design": True,  # Keep original
            "spec": original_spec,
            "css_variables": _generate_css_variables(original_spec),
            "issues": ["Design regeneration failed — keeping original spec"],
            "tokens": inp_tok + out_tok,
            "from_cache": False,
            "regenerated": False,
        }

    # Preserve data_testid values from original to maintain interaction test compatibility
    original_testids = _extract_testids(original_spec)
    new_testids = _extract_testids(spec)

    # Merge: use original testids where possible, keep new ones for new components
    for comp_name, testids in original_testids.items():
        for new_comp in spec.get("components", []):
            if new_comp["name"] == comp_name and "data_testid" not in new_comp:
                new_comp["data_testid"] = testids.get("root", comp_name.lower().replace(" ", "-"))

    css_vars = _generate_css_variables(spec)

    logger.info(
        "design_regenerator.complete",
        execution_id=execution_id,
        components=len(spec.get("components", [])),
        tokens=inp_tok + out_tok,
    )

    return {
        "has_design": True,
        "spec": spec,
        "css_variables": css_vars,
        "issues": [],
        "tokens": inp_tok + out_tok,
        "from_cache": False,
        "regenerated": True,
    }


def _parse_design_json(content: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from model output."""
    import re
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```json\s*(.*?)```', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    match = re.search(r'(\{.*\})', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _generate_css_variables(spec: Dict[str, Any]) -> str:
    """Generate CSS :root block from spec."""
    ds = spec.get("design_system", {})
    colors = ds.get("colors", {})
    typography = ds.get("typography", {})
    radii = ds.get("radii", {})
    breakpoints = spec.get("responsive_breakpoints", {})

    lines = [":root {"]
    for k, v in colors.items():
        lines.append(f"  --color-{k.replace('_', '-')}: {v};")
    if "font_family" in typography:
        lines.append(f"  --font-family: {typography['font_family']};")
    for k, v in typography.get("heading_sizes", {}).items():
        lines.append(f"  --font-size-{k}: {v};")
    if "body_size" in typography:
        lines.append(f"  --font-size-body: {typography['body_size']};")
    if "line_height" in typography:
        lines.append(f"  --line-height: {typography['line_height']};")
    for k, v in radii.items():
        lines.append(f"  --radius-{k}: {v};")
    for k, v in breakpoints.items():
        lines.append(f"  --breakpoint-{k}: {v};")
    lines.append("}")

    return "\n".join(lines)


def _extract_testids(spec: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Extract testid mapping from spec."""
    testids = {}
    for comp in spec.get("components", []):
        name = comp["name"]
        base = comp.get("data_testid", name.lower().replace(" ", "-"))
        testids[name] = {
            "root": base,
            "trigger": f"{base}-trigger",
            "content": f"{base}-content",
        }
    return testids


def score_design_failure(failure: Dict[str, Any]) -> float:
    """
    Return the raw design-level confidence score (0.0–1.0) for a failure.
    Useful for logging, debugging, or applying a custom threshold.

    Score accumulates from weighted keyword matches:
      - Strong design signals (z-index, overflow, layout clipping): +0.4 each
      - Medium design signals (color, typography, spacing, responsive): +0.25 each
      - Weak design signals (state, animation, transition): +0.15 each
      - Code-level counter-signals (syntax, import, TypeError, etc.): -0.3 each

    Scores are clamped to [0.0, 1.0].
    """
    description = failure.get("description", "").lower()
    score = 0.0

    # Strong signals — almost certainly a spec-level issue
    strong_design = [
        "z-index", "clip", "overflow", "position", "layout",
        "stacking", "modal clipped", "hidden behind", "obscured by",
    ]
    # Medium signals — likely design, possibly code
    medium_design = [
        "contrast", "color", "typography", "font", "spacing",
        "breakpoint", "responsive", "grid", "flex", "padding",
        "margin", "alignment", "border", "radius", "shadow",
    ]
    # Weak signals — could go either way
    weak_design = [
        "state", "animation", "transition", "hover", "focus",
        "visible", "hidden", "display",
    ]
    # Counter-signals — code-level problems
    code_signals = [
        "syntax", "import", "undefined", "typeerror", "referenceerror",
        "cannot read", "is not a function", "unexpected token",
        "module not found", "missing dependency",
    ]

    for kw in strong_design:
        if kw in description:
            score += 0.4
    for kw in medium_design:
        if kw in description:
            score += 0.25
    for kw in weak_design:
        if kw in description:
            score += 0.15
    for kw in code_signals:
        if kw in description:
            score -= 0.3

    return max(0.0, min(1.0, score))


def is_design_level_failure(failure: Dict[str, Any], threshold: float = 0.5) -> bool:
    """
    Determine if a failure is design-level (needs spec regeneration)
    vs code-level (coder can fix without changing spec).

    v2.9: Uses a scored classifier instead of a binary keyword check.
    v2.10: Delegates entirely to score_design_failure — single source of truth
           for weights and keyword lists. Pass threshold from TenantConfig
           (config.design_failure_threshold) for per-tenant tuning.
    """
    return score_design_failure(failure) >= threshold
