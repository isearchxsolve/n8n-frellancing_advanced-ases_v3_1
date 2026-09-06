"""
ASES - Requirement Clarifier (Gap Fix #3)
==========================================
Solves: Underspecified tasks that cause the planner to make wrong assumptions,
leading to a technically correct but functionally wrong deliverable.

Problem in v2.5:
    Task: "Build a dashboard"
    Planner assumes: React SPA with charts
    Client wanted: Admin panel with user management
    Tests pass on iteration 1. Wrong product delivered.

    There is currently no pre-flight check on task clarity. The system
    accepts any string and runs the full pipeline.

Solution:
    clarifier_agent() runs BEFORE the planner. It:
    1. Scores the task on 4 specificity dimensions (0-10 each)
    2. If the total score < CLARITY_THRESHOLD, returns CLARIFICATION_NEEDED
       with a ranked list of questions
    3. If the task is clear enough, returns PROCEED with inferred assumptions
       injected back into the requirements string

    The caller (main.py route handler or n8n webhook) decides whether to:
    - Block and return questions to the user (interactive mode)
    - Proceed with inferred assumptions logged (autonomous mode)
    - Use a per-tenant threshold to control when to block vs infer

Integration:
    In agent_loop.py _dev_pipeline(), before planner_agent():

        clarity = await clarifier_agent(task, tech_stack, requirements, config, execution_id)

        if clarity["action"] == "CLARIFICATION_NEEDED" and config.require_clarity:
            return {
                "success": False,
                "clarification_needed": True,
                "questions": clarity["questions"],
                "clarity_score": clarity["score"],
            }

        # Augment requirements with inferred assumptions either way
        requirements = clarity["augmented_requirements"]

    In TenantConfig (models.py), add:
        require_clarity: bool = False       # set True for interactive tenants
        clarity_threshold: float = 5.0      # 0-10, below this = ask questions
"""

from typing import Dict, Any, List
import json
import structlog

logger = structlog.get_logger()

CLARITY_THRESHOLD_DEFAULT = 5.0


async def clarifier_agent(
    task: str,
    tech_stack: str,
    requirements: str,
    config,                  # TenantConfig
    execution_id: str,
) -> Dict[str, Any]:
    """
    Analyses a task description for ambiguity before the planner runs.

    Returns:
    {
        "action": "PROCEED" | "CLARIFICATION_NEEDED",
        "score": 6.5,                    # 0-10, higher = clearer
        "dimensions": { ... },           # per-dimension breakdown
        "questions": [...],              # ranked clarifying questions
        "inferred_assumptions": [...],   # what the system will assume
        "augmented_requirements": "...", # requirements + inferred assumptions
    }
    """
    from agent_loop import call_model

    system_prompt = """You are a senior technical project manager reviewing a software development task brief.

Score the task on these 4 dimensions (0-10 each):
1. SCOPE_CLARITY: Is the feature set well-defined? (10 = exhaustive list, 0 = "build a thing")
2. DATA_MODEL: Are entities, relationships, and data flows specified? (10 = full schema described)
3. AUTH_REQUIREMENTS: Is auth/permissions/roles clear? (10 = explicit, 0 = not mentioned)
4. ACCEPTANCE_CRITERIA: Are success conditions testable and measurable? (10 = given/when/then, 0 = "make it work")

Then:
- Identify the 3 most critical missing pieces of information
- State what a senior dev would ASSUME for each missing piece
- Write an augmented requirements string that includes those assumptions

Output ONLY valid JSON:
{
  "dimensions": {
    "scope_clarity": 7,
    "data_model": 4,
    "auth_requirements": 2,
    "acceptance_criteria": 6
  },
  "total_score": 4.75,
  "questions": [
    {"priority": 1, "question": "...", "impact": "..."},
    {"priority": 2, "question": "...", "impact": "..."},
    {"priority": 3, "question": "...", "impact": "..."}
  ],
  "inferred_assumptions": [
    "Auth: JWT-based authentication, single user role unless specified",
    "Data model: PostgreSQL with UUID primary keys",
    "Acceptance: API endpoints return 200 on success, 4xx on validation errors"
  ],
  "augmented_requirements": "Original requirements plus: [assumption 1]. [assumption 2]. [assumption 3]."
}"""

    user_prompt = f"""Task: {task}
Tech Stack: {tech_stack}
Requirements provided: {requirements or "(none)"}

Analyse and score this task."""

    try:
        content, inp_tok, out_tok = await call_model(
            model=config.reviewer_model,   # cheap model — this is pre-flight
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=800,
            execution_id=execution_id,
            call_type="reviewer",
        )

        result = _parse_json_safe(content)

        if not result:
            # Parsing failed — proceed with original requirements, log warning
            logger.warning("clarifier.parse_failed", execution_id=execution_id)
            return _fallback(task, requirements)

        score = float(result.get("total_score", 5.0))
        threshold = getattr(config, "clarity_threshold", CLARITY_THRESHOLD_DEFAULT)
        action = "CLARIFICATION_NEEDED" if score < threshold else "PROCEED"

        logger.info(
            "clarifier.complete",
            execution_id=execution_id,
            score=score,
            action=action,
            threshold=threshold,
        )

        return {
            "action": action,
            "score": score,
            "dimensions": result.get("dimensions", {}),
            "questions": result.get("questions", []),
            "inferred_assumptions": result.get("inferred_assumptions", []),
            "augmented_requirements": result.get("augmented_requirements", requirements),
            "tokens": inp_tok + out_tok,
        }

    except Exception as e:
        logger.warning("clarifier.failed", execution_id=execution_id, error=str(e))
        return _fallback(task, requirements)


def _parse_json_safe(content: str) -> Dict:
    """Parse JSON, stripping markdown fences if present."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        import re
        match = re.search(r'```json\s*(.*?)```', content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return {}


def _fallback(task: str, requirements: str) -> Dict[str, Any]:
    """Safe default when the clarifier itself fails — always PROCEED."""
    return {
        "action": "PROCEED",
        "score": 5.0,
        "dimensions": {},
        "questions": [],
        "inferred_assumptions": [],
        "augmented_requirements": requirements,
        "tokens": 0,
    }


# ---------------------------------------------------------------------------
# models.py additions (add these fields to TenantConfig)
# ---------------------------------------------------------------------------

TENANTCONFIG_ADDITIONS = """
# Add to TenantConfig in models.py:

require_clarity: bool = False
# When True, jobs with clarity_score < clarity_threshold are blocked and
# return questions to the caller instead of running the pipeline.
# Set False for fully autonomous mode (questions are logged but not blocking).

clarity_threshold: float = 5.0
# 0-10. Tasks scoring below this trigger CLARIFICATION_NEEDED.
# Raise to 7.0 for high-stakes tenants. Lower to 3.0 for autonomous batch jobs.
"""
