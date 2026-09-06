"""
ASES - Billing Enforcer
Active cost enforcement: checks limits BEFORE spending tokens, not after.

Provides:
  - Per-tenant daily / monthly spend caps
  - Pre-flight budget check before each job
  - Mid-job cost fence (called between agent iterations)
  - Soft warning threshold (80 %) + hard kill threshold (100 %)
  - Aggregated spend written to Postgres for billing reports

Usage in agent_loop.py:
    from billing import BillingFence, BillingLimitError

    fence = BillingFence(tenant_id, config, pool)
    await fence.preflight()             # raises if already over limit

    # ...inside iteration loop...
    await fence.checkpoint(tokens_used, cost_usd)   # raises on overage
"""

import os
import json
from typing import Optional
from datetime import datetime, date

import asyncpg
import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BillingLimitError(Exception):
    """Raised when a cost or token limit would be exceeded."""
    def __init__(self, reason: str, limit_type: str, current: float, limit: float):
        self.reason     = reason
        self.limit_type = limit_type
        self.current    = current
        self.limit      = limit
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Limit resolution helpers
# ---------------------------------------------------------------------------

# Default plan limits (USD per day / per month)
_PLAN_LIMITS = {
    "free":       {"daily_usd": 0.50,  "monthly_usd": 5.00,  "job_usd": 0.50},
    "pro":        {"daily_usd": 10.00, "monthly_usd": 100.00, "job_usd": 2.00},
    "enterprise": {"daily_usd": 200.00,"monthly_usd": 3000.00,"job_usd": 10.00},
}

DEFAULT_LIMITS = _PLAN_LIMITS["free"]


def get_plan_limits(plan: str) -> dict:
    return _PLAN_LIMITS.get(plan, DEFAULT_LIMITS)


# ---------------------------------------------------------------------------
# Spend queries
# ---------------------------------------------------------------------------

async def get_daily_spend(pool: asyncpg.Pool, tenant_id: str) -> float:
    today = date.today().isoformat()
    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM executions e
        JOIN tenants t ON t.id = e.tenant_id
        WHERE t.slug = $1
          AND DATE(e.completed_at) = $2
          AND e.success = true
        """,
        tenant_id, today,
    )
    return float(row["total"]) if row else 0.0


async def get_monthly_spend(pool: asyncpg.Pool, tenant_id: str) -> float:
    today = datetime.utcnow()
    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM executions e
        JOIN tenants t ON t.id = e.tenant_id
        WHERE t.slug = $1
          AND DATE_TRUNC('month', e.completed_at) = DATE_TRUNC('month', NOW()::timestamp)
          AND e.success = true
        """,
        tenant_id,
    )
    return float(row["total"]) if row else 0.0


async def record_spend(
    pool: asyncpg.Pool,
    tenant_id: str,
    execution_id: str,
    cost_usd: float,
    tokens: int,
) -> None:
    """Upsert spend record so billing is always consistent with execution table."""
    try:
        tenant_uuid = await pool.fetchval(
            "SELECT id FROM tenants WHERE slug = $1", tenant_id
        )
        if not tenant_uuid:
            return
        await pool.execute(
            """
            INSERT INTO billing_events (tenant_id, execution_id, cost_usd, tokens, recorded_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (execution_id) DO UPDATE SET
                cost_usd = EXCLUDED.cost_usd,
                tokens   = EXCLUDED.tokens
            """,
            tenant_uuid, execution_id, cost_usd, tokens,
        )
    except Exception as e:
        logger.error("billing.record_spend_failed", error=str(e), execution_id=execution_id)


# ---------------------------------------------------------------------------
# BillingFence — stateful per-job enforcer
# ---------------------------------------------------------------------------

class BillingFence:
    """
    Stateful billing guard attached to a single job execution.

    Lifecycle:
      1. await fence.preflight()           — check aggregate limits before any LLM call
      2. await fence.checkpoint(tok, cost) — call after each agent iteration
      3. await fence.finalize(tok, cost)   — record final spend

    All three methods raise BillingLimitError on violation.
    """

    def __init__(
        self,
        tenant_id: str,
        execution_id: str,
        plan: str,
        job_cost_limit_usd: float,
        job_token_budget: int,
        pool: asyncpg.Pool,
    ):
        self.tenant_id          = tenant_id
        self.execution_id       = execution_id
        self.plan               = plan
        self.pool               = pool
        self.plan_limits        = get_plan_limits(plan)
        self.job_cost_limit     = min(job_cost_limit_usd, self.plan_limits["job_usd"])
        self.job_token_budget   = job_token_budget
        self._warned            = False   # soft warning already emitted?

    # --- Public interface ---

    async def preflight(self) -> None:
        """Check aggregate spend before the job starts. Raises if already over."""
        daily   = await get_daily_spend(self.pool, self.tenant_id)
        monthly = await get_monthly_spend(self.pool, self.tenant_id)

        if daily >= self.plan_limits["daily_usd"]:
            raise BillingLimitError(
                f"Daily spend limit reached (${daily:.2f} / ${self.plan_limits['daily_usd']:.2f}). "
                "Resets at midnight UTC.",
                limit_type="daily_usd",
                current=daily,
                limit=self.plan_limits["daily_usd"],
            )

        if monthly >= self.plan_limits["monthly_usd"]:
            raise BillingLimitError(
                f"Monthly spend limit reached (${monthly:.2f} / ${self.plan_limits['monthly_usd']:.2f}).",
                limit_type="monthly_usd",
                current=monthly,
                limit=self.plan_limits["monthly_usd"],
            )

        logger.info(
            "billing.preflight_passed",
            tenant_id=self.tenant_id,
            daily_spend=daily,
            monthly_spend=monthly,
            job_limit=self.job_cost_limit,
        )

    async def checkpoint(self, tokens_used: int, cost_usd: float) -> None:
        """
        Called between agent iterations.
        Raises BillingLimitError if the job has exceeded per-job limits.
        Emits a structured warning at 80 % of the budget.
        """
        # Token budget
        if tokens_used > self.job_token_budget:
            raise BillingLimitError(
                f"Token budget exceeded ({tokens_used:,} / {self.job_token_budget:,}).",
                limit_type="token_budget",
                current=tokens_used,
                limit=self.job_token_budget,
            )

        # Cost limit
        if cost_usd >= self.job_cost_limit:
            raise BillingLimitError(
                f"Job cost limit reached (${cost_usd:.4f} / ${self.job_cost_limit:.4f}).",
                limit_type="job_cost_usd",
                current=cost_usd,
                limit=self.job_cost_limit,
            )

        # Soft warning at 80 %
        if not self._warned and cost_usd >= self.job_cost_limit * 0.80:
            self._warned = True
            logger.warning(
                "billing.soft_limit_warning",
                tenant_id=self.tenant_id,
                execution_id=self.execution_id,
                cost_usd=cost_usd,
                limit=self.job_cost_limit,
                pct=round(cost_usd / self.job_cost_limit * 100, 1),
            )
            # Optionally emit a Prometheus counter
            try:
                from observability import metrics
                if metrics:
                    metrics.billing_enforced.labels(
                        tenant_id=self.tenant_id, reason="soft_warning"
                    ).inc()
            except Exception:
                pass

    async def finalize(self, tokens_used: int, cost_usd: float) -> None:
        """Record the final spend regardless of success/failure."""
        await record_spend(self.pool, self.tenant_id, self.execution_id, cost_usd, tokens_used)

        try:
            from observability import metrics
            if metrics:
                metrics.job_cost.labels(
                    tenant_id=self.tenant_id, task_type="finalized"
                ).observe(cost_usd)
        except Exception:
            pass

        logger.info(
            "billing.finalized",
            tenant_id=self.tenant_id,
            execution_id=self.execution_id,
            cost_usd=cost_usd,
            tokens=tokens_used,
        )
