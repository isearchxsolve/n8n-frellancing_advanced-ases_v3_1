"""
ASES - Design A/B Tester (v2.7)
=================================
A/B tests design specifications from vector memory to optimize for
visual review pass rate. Replaces random spec selection with
multi-armed bandit optimization.

Problem in v2.6:
    Vector memory retrieves the single highest-similarity design spec.
    If that spec consistently fails visual review (e.g., dark mode specs
    fail on low-contrast displays), the system keeps using it because
    it has the highest hit_count.

Solution:
    1. Retrieve top-N similar specs (not just top-1)
    2. Track pass/fail rate per spec (from journal data)
    3. Use epsilon-greedy bandit to balance exploration vs exploitation
    4. Gradually shift traffic to specs with higher pass rates

Integration: design_agent.py — replaces simple highest-similarity retrieval
with bandit-optimized selection.
"""

import json
import random
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

logger = structlog.get_logger()


@dataclass
class DesignVariant:
    spec_id: str
    spec_json: Dict[str, Any]
    similarity: float
    hit_count: int
    pass_count: int
    fail_count: int
    last_used: Optional[datetime]
    pass_rate: float = 0.0

    def __post_init__(self):
        total = self.pass_count + self.fail_count
        if total > 0:
            self.pass_rate = self.pass_count / total
        else:
            # New specs get a small prior to encourage exploration
            self.pass_rate = 0.5


class DesignABTester:
    """
    Multi-armed bandit for design spec selection.

    Epsilon-greedy strategy:
    - With probability epsilon: explore (random untested variant)
    - With probability 1-epsilon: exploit (highest pass_rate variant)

    Epsilon decays over time as we gather more data.
    """

    def __init__(
        self,
        epsilon: float = 0.3,           # Initial exploration rate
        epsilon_decay: float = 0.95,     # Decay per day
        min_epsilon: float = 0.05,       # Minimum exploration
    ):
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.min_epsilon = min_epsilon

    async def select_variant(
        self,
        pool,
        tenant_uuid: str,
        task: str,
        tech_stack: str,
        execution_id: str,
    ) -> Optional[tuple]:
        """
        Select the best design spec variant using bandit optimization.

        Returns:
            (spec_dict, spec_id) tuple, or (None, None) if no variants available.
        """
        variants = await self._load_variants(pool, tenant_uuid, task, tech_stack)

        if not variants:
            return None, None

        if len(variants) == 1:
            logger.info("design_ab.only_variant", execution_id=execution_id)
            return variants[0].spec_json, variants[0].spec_id

        # Apply epsilon-greedy selection
        current_epsilon = self._get_current_epsilon(variants)

        if random.random() < current_epsilon:
            # Explore: pick least-tested variant
            selected = min(variants, key=lambda v: v.pass_count + v.fail_count)
            logger.info(
                "design_ab.explore",
                execution_id=execution_id,
                variant=selected.spec_id,
                epsilon=current_epsilon,
            )
        else:
            # Exploit: pick highest pass rate
            selected = max(variants, key=lambda v: v.pass_rate)
            logger.info(
                "design_ab.exploit",
                execution_id=execution_id,
                variant=selected.spec_id,
                pass_rate=selected.pass_rate,
                epsilon=current_epsilon,
            )

        return selected.spec_json, selected.spec_id

    async def record_result(
        self,
        pool,
        spec_id: str,
        passed: bool,
        execution_id: str,
    ) -> None:
        """Record pass/fail result for a variant."""
        try:
            await pool.execute(
                """
                UPDATE design_specs
                SET pass_count = pass_count + $1,
                    fail_count = fail_count + $2,
                    last_used = NOW()
                WHERE id = $3
                """,
                1 if passed else 0,
                0 if passed else 1,
                int(spec_id),
            )
            logger.info(
                "design_ab.result_recorded",
                execution_id=execution_id,
                spec_id=spec_id,
                passed=passed,
            )
        except Exception as e:
            logger.warning("design_ab.record_failed", error=str(e))

    async def _load_variants(
        self,
        pool,
        tenant_uuid: str,
        task: str,
        tech_stack: str,
    ) -> List[DesignVariant]:
        """Load top-N similar variants with their performance stats."""
        try:
            from vector_memory import _embed

            query_embedding = await _embed(f"Task: {task}\nStack: {tech_stack}")

            if query_embedding is None:
                return []

            rows = await pool.fetch(
                """
                SELECT id, spec_json, 1 - (embedding <=> $2::vector) AS similarity,
                       hit_count, pass_count, fail_count, last_used
                FROM design_specs
                WHERE tenant_id = $1
                  AND tech_stack = $3
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $2::vector) >= 0.65
                ORDER BY similarity DESC
                LIMIT 5
                """,
                tenant_uuid,
                json.dumps(query_embedding),
                tech_stack,
            )

            variants = []
            for row in rows:
                spec = json.loads(row["spec_json"])
                variants.append(DesignVariant(
                    spec_id=str(row["id"]),
                    spec_json=spec,
                    similarity=float(row["similarity"]),
                    hit_count=row["hit_count"],
                    pass_count=row.get("pass_count", 0),
                    fail_count=row.get("fail_count", 0),
                    last_used=row.get("last_used"),
                ))

            return variants

        except Exception as e:
            logger.warning("design_ab.load_failed", error=str(e))
            return []

    def _get_current_epsilon(self, variants: List[DesignVariant]) -> float:
        """Calculate decayed epsilon based on total trials."""
        total_trials = sum(v.pass_count + v.fail_count for v in variants)

        # Decay epsilon based on total trials
        decayed = self.epsilon * (self.epsilon_decay ** (total_trials / 10))
        return max(decayed, self.min_epsilon)


# Convenience function for design_agent.py integration
async def select_design_spec_with_ab_test(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    execution_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Select a design spec using A/B testing.

    Returns:
        (spec_dict, spec_id) or (None, None)

    v2.9: select_variant returns (spec, spec_id) directly — no redundant embed/query.
    """
    tester = DesignABTester()
    spec, spec_id = await tester.select_variant(pool, tenant_uuid, task, tech_stack, execution_id)
    return spec, spec_id


async def select_best_fallback_spec(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    execution_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Select the best fallback spec when regen cap is reached.

    v2.10: Uses blended score (similarity * 0.7 + pass_rate * 0.3) rather than
    pass_rate alone. This prevents a high-performing spec for a different task
    context from winning over a moderately-performing but contextually-matched one.
    """
    tester = DesignABTester()
    variants = await tester._load_variants(pool, tenant_uuid, task, tech_stack)

    if not variants:
        return None, None

    if len(variants) == 1:
        return variants[0].spec_json, variants[0].spec_id

    def _blended(v: DesignVariant) -> float:
        # Clamp defensively — similarity is cosine-based so should be [-1,1],
        # pass_rate is [0,1] from pass_count/total, but guard against future changes.
        sim = max(0.0, min(float(v.similarity), 1.0))
        pr  = max(0.0, min(float(v.pass_rate), 1.0))
        return sim * 0.7 + pr * 0.3

    best = max(variants, key=_blended)
    logger.info(
        "design_ab.fallback_selected",
        execution_id=execution_id,
        variant=best.spec_id,
        similarity=round(max(0.0, min(float(best.similarity), 1.0)), 3),
        pass_rate=round(max(0.0, min(float(best.pass_rate), 1.0)), 3),
        blended=round(_blended(best), 3),
    )
    return best.spec_json, best.spec_id
