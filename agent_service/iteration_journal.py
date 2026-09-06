"""
ASES - Iteration Journal v2 (Gap Fix: journal KEEP list saturation)
=====================================================================
Replaces the flat-append constraint list with a scored priority queue.
Constraints earn/lose confidence over iterations; only top-N by score are injected.

Problem with flat list:
    Every passing iteration appends 3-5 constraints. On a 10-iteration run:
    up to 50 constraints injected with equal weight -> model ignores all of them.

Solution — scored priority queue:
    Each constraint carries:
        score     = confirmed * CONFIRM_WEIGHT - violated * VIOLATE_PENALTY
        confirmed = how many passing iterations included this decision
        violated  = how many iterations semantic_differ flagged a regression

    Only top MAX_INJECT constraints (by score) are injected.
    Constraints below MIN_SCORE are pruned from the queue entirely.

v2.6 additions:
- Design decision tracking: tracks which design spec choices survive visual review
- Visual review feedback integration: penalizes design decisions that fail visual gate
- Interaction test tracking: penalizes design decisions that fail interaction review
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

import structlog

logger = structlog.get_logger()

CONFIRM_WEIGHT  = 1.0
VIOLATE_PENALTY = 2.5
MAX_INJECT      = 8
MIN_SCORE       = -3.0


@dataclass
class ScoredConstraint:
    text: str
    score: float = 1.0
    first_seen: int = 1
    last_seen: int = 1
    confirmed: int = 1
    violated: int = 0
    key: str = ""
    category: str = "code"       # NEW: "code" | "design" | "interaction"

    def __post_init__(self):
        if not self.key:
            self.key = self.text.lower()[:60]


@dataclass
class IterationRecord:
    iteration: int
    file_hashes: Dict[str, str]
    file_paths: List[str]
    test_passed: bool
    static_passed: Optional[bool]
    reviewer_approved: Optional[bool]
    visual_approved: Optional[bool]       # NEW
    interaction_approved: Optional[bool]  # NEW
    architectural_decisions: List[str]
    design_decisions: List[str]           # NEW
    errors: List[str]


class IterationJournal:
    def __init__(self, task: str, tech_stack: str):
        self.task = task
        self.tech_stack = tech_stack
        self.records: List[IterationRecord] = []
        self._constraints: Dict[str, ScoredConstraint] = {}
        self._design_constraints: Dict[str, ScoredConstraint] = {}  # NEW

    async def record(self, iteration, files, test_results, config, execution_id,
                     static_result=None, review_result=None,
                     visual_result=None, interaction_result=None,    # NEW
                     design_spec=None):                               # NEW
        """
        Record an iteration with full context including design and interaction results.
        """
        file_hashes = {
            f["path"]: hashlib.sha256(f["content"].encode()).hexdigest()[:12]
            for f in files
        }
        errors = []
        if not test_results.get("success"):
            raw = test_results.get("stderr") or test_results.get("stdout", "")
            errors.append(f"TEST_FAIL: {raw[:500]}")
        if static_result and not static_result.get("approved"):
            errors += static_result.get("issues_flat", [])[:5]
        if review_result:
            rev = review_result.get("review", {})
            if not rev.get("approved"):
                errors += rev.get("issues_flat", [])[:5]

        # NEW: Visual review errors
        if visual_result and not visual_result.get("approved"):
            errors.append(f"VISUAL_FAIL: {visual_result.get('issues_text', 'Visual review failed')[:300]}")

        # NEW: Interaction review errors
        if interaction_result and not interaction_result.get("approved"):
            failures = interaction_result.get("failures", [])
            for f in failures[:3]:
                errors.append(f"INTERACTION_FAIL: {f.get('name', 'unknown')} — {f.get('error', 'unknown')[:200]}")

        decisions = await self._extract_decisions(files, config, execution_id)

        # NEW: Extract design decisions from spec
        design_decisions = []
        if design_spec and design_spec.get("has_design"):
            design_decisions = self._extract_design_decisions(design_spec["spec"])

        rec = IterationRecord(
            iteration=iteration,
            file_hashes=file_hashes,
            file_paths=list(file_hashes.keys()),
            test_passed=test_results.get("success", False),
            static_passed=static_result.get("approved") if static_result else None,
            reviewer_approved=(
                review_result.get("review", {}).get("approved") if review_result else None
            ),
            visual_approved=visual_result.get("approved") if visual_result else None,
            interaction_approved=interaction_result.get("approved") if interaction_result else None,
            architectural_decisions=decisions,
            design_decisions=design_decisions,
            errors=errors,
        )
        self.records.append(rec)

        if rec.test_passed:
            self._update_constraints(decisions, iteration)
            # NEW: Update design constraints
            self._update_design_constraints(design_decisions, iteration)
            self._prune()

        logger.info("journal.recorded", execution_id=execution_id, iteration=iteration,
                    decisions=len(decisions), design_decisions=len(design_decisions),
                    constraints=len(self._constraints), 
                    design_constraints=len(self._design_constraints))

    def penalise_violated(self, broken_imports: List[str]) -> None:
        """Penalise constraints whose decisions appear in a regression report."""
        if not broken_imports:
            return
        for key, c in self._constraints.items():
            words = set(c.text.lower().split())
            for broken in broken_imports:
                if len(words & set(broken.lower().split())) >= 2:
                    c.violated += 1
                    c.score = c.confirmed * CONFIRM_WEIGHT - c.violated * VIOLATE_PENALTY
                    logger.info("journal.constraint_penalised", key=key, score=round(c.score, 2))
                    break
        self._prune()

    # NEW: Penalize design decisions that failed visual/interaction review
    def penalise_design_failure(self, design_decisions: List[str], failure_type: str) -> None:
        """
        Penalize design decisions that caused visual or interaction failures.

        Args:
            design_decisions: List of design decision texts that were present
            failure_type: "visual" or "interaction"
        """
        if not design_decisions:
            return

        penalty_multiplier = 3.0 if failure_type == "interaction" else 2.0

        for key, c in self._design_constraints.items():
            for decision in design_decisions:
                if decision.lower()[:60] == c.key or decision.lower() in c.text.lower():
                    c.violated += 1
                    c.score = c.confirmed * CONFIRM_WEIGHT - c.violated * VIOLATE_PENALTY * penalty_multiplier
                    logger.info(
                        "journal.design_constraint_penalised",
                        key=key,
                        score=round(c.score, 2),
                        failure_type=failure_type,
                    )
                    break
        self._prune_design()

    def build_context_block(self) -> str:
        if not self.records:
            return ""

        top = self._top_constraints()
        top_design = self._top_design_constraints()  # NEW

        lines = ["\n\n=== ARCHITECTURAL JOURNAL (read before writing any code) ==="]

        if top:
            lines.append("\nDO NOT REGRESS — confirmed architectural decisions:")
            for c in top:
                confidence = "HIGH" if c.score >= 3.0 else "MED" if c.score >= 1.5 else "LOW"
                lines.append(f"  • [KEEP/{confidence}] {c.text}")

        # NEW: Design constraints section
        if top_design:
            lines.append("\nDESIGN SYSTEM CONSTRAINTS — these visual decisions survived review:")
            for c in top_design:
                confidence = "HIGH" if c.score >= 3.0 else "MED" if c.score >= 1.5 else "LOW"
                lines.append(f"  • [DESIGN/{confidence}] {c.text}")

        for rec in self.records[-3:]:
            status = "TESTS PASSED" if rec.test_passed else "TESTS FAILED"
            lines.append(f"\nIteration {rec.iteration} — {status}")
            lines.append(f"  Files: {', '.join(rec.file_paths)}")
            if rec.architectural_decisions:
                lines.append(f"  Decisions: {'; '.join(rec.architectural_decisions)}")
            if rec.design_decisions:
                lines.append(f"  Design: {'; '.join(rec.design_decisions)}")
            if rec.errors:
                lines.append("  Errors to fix:")
                for e in rec.errors[:3]:
                    lines.append(f"    - {e}")

        lines.append("\n=== END JOURNAL ===")
        return "\n".join(lines)

    def detect_regressions(self, current_files, current_test_passed):
        last_passing = next((r for r in reversed(self.records) if r.test_passed), None)
        if not last_passing:
            return []
        current_hashes = {
            f["path"]: hashlib.sha256(f["content"].encode()).hexdigest()[:12]
            for f in current_files
        }
        return [
            path for path, old_hash in last_passing.file_hashes.items()
            if current_hashes.get(path) and current_hashes[path] != old_hash
        ]

    # ---- internals ----

    def _update_constraints(self, decisions, iteration):
        for decision in decisions:
            key = decision.lower()[:60]
            if key in self._constraints:
                c = self._constraints[key]
                c.last_seen = iteration
                c.confirmed += 1
                c.score = c.confirmed * CONFIRM_WEIGHT - c.violated * VIOLATE_PENALTY
            else:
                self._constraints[key] = ScoredConstraint(
                    text=decision, score=1.0, first_seen=iteration, last_seen=iteration,
                    confirmed=1, violated=0, key=key, category="code",
                )

    # NEW: Update design constraints
    def _update_design_constraints(self, decisions, iteration):
        for decision in decisions:
            key = decision.lower()[:60]
            if key in self._design_constraints:
                c = self._design_constraints[key]
                c.last_seen = iteration
                c.confirmed += 1
                c.score = c.confirmed * CONFIRM_WEIGHT - c.violated * VIOLATE_PENALTY
            else:
                self._design_constraints[key] = ScoredConstraint(
                    text=decision, score=1.0, first_seen=iteration, last_seen=iteration,
                    confirmed=1, violated=0, key=key, category="design",
                )

    def _top_constraints(self):
        valid = [c for c in self._constraints.values() if c.score > 0]
        return sorted(valid, key=lambda c: c.score, reverse=True)[:MAX_INJECT]

    # NEW: Top design constraints
    def _top_design_constraints(self):
        valid = [c for c in self._design_constraints.values() if c.score > 0]
        return sorted(valid, key=lambda c: c.score, reverse=True)[:MAX_INJECT]

    def _prune(self):
        before = len(self._constraints)
        self._constraints = {k: v for k, v in self._constraints.items() if v.score > MIN_SCORE}
        if pruned := before - len(self._constraints):
            logger.info("journal.constraints_pruned", count=pruned)

    # NEW: Prune design constraints
    def _prune_design(self):
        before = len(self._design_constraints)
        self._design_constraints = {k: v for k, v in self._design_constraints.items() if v.score > MIN_SCORE}
        if pruned := before - len(self._design_constraints):
            logger.info("journal.design_constraints_pruned", count=pruned)

    async def _extract_decisions(self, files, config, execution_id):
        summary_files = [
            f for f in files
            if any(k in f["path"] for k in ["index", "main", "app", "package.json", "requirements"])
        ][:2] or files[:1]

        compact = "\n\n".join(f"FILE: {f['path']}\n{f['content'][:600]}" for f in summary_files)

        prompt = f"""Analyse this code and list 3-5 key architectural decisions made.
Focus on: auth strategy, data storage, API patterns, error handling, key libraries.
One line each. Tech stack: {self.tech_stack}. Task: {self.task}.

Code:
{compact}

Output ONLY a JSON array of strings."""

        try:
            from agent_loop import call_model
            content, _, _ = await call_model(
                model=config.reviewer_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=300,
                execution_id=execution_id, call_type="reviewer",
            )
            content = content.strip().lstrip("```json").rstrip("```").strip()
            decisions = json.loads(content)
            if isinstance(decisions, list):
                return [str(d) for d in decisions[:5]]
        except Exception as e:
            logger.warning("journal.decision_extract_failed", error=str(e))
        return []

    # NEW: Extract design decisions from spec
    def _extract_design_decisions(self, spec: Dict[str, Any]) -> List[str]:
        """Extract key design decisions from a design spec for journal tracking."""
        decisions = []

        ds = spec.get("design_system", {})
        colors = ds.get("colors", {})
        if colors:
            decisions.append(f"Color system: primary={colors.get('primary', 'unset')}, background={colors.get('background', 'unset')}")

        typography = ds.get("typography", {})
        if typography:
            decisions.append(f"Typography: {typography.get('font_family', 'unset')}, body={typography.get('body_size', 'unset')}")

        layout = spec.get("layout", {})
        if layout:
            decisions.append(f"Layout: {layout.get('grid_columns', 12)}-col grid, max-width={layout.get('max_width', 'unset')}")

        for component in spec.get("components", [])[:3]:
            name = component["name"]
            states = component.get("states", [])
            if states:
                decisions.append(f"{name} states: {', '.join(states)}")
            interactions = component.get("interaction_rules", [])
            if interactions:
                decisions.append(f"{name} interactions: {len(interactions)} rules")

        return decisions
