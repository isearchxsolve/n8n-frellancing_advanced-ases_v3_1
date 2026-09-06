"""
ASES - Database Layer
Async PostgreSQL via asyncpg.
Handles: tenant config, execution persistence, sandbox state registry.
"""

import os
import json
from typing import Any, Dict, Optional, List
from datetime import datetime, timedelta

import asyncpg
import structlog

from models import TenantConfig

logger = structlog.get_logger()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://ases:ases_secure_password@postgres:5432/ases_production",
)

_pool: Optional[asyncpg.Pool] = None


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("db.pool_created")
    return _pool


async def close_db_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Tenant config
# ---------------------------------------------------------------------------

async def get_tenant_config_from_db(pool: asyncpg.Pool, tenant_id: str) -> TenantConfig:
    """
    Fetch tenant config from DB. Falls back to safe defaults if tenant not found.
    Creates a default tenant row on first call so the system self-bootstraps.
    """
    row = await pool.fetchrow(
        "SELECT config, plan, status FROM tenants WHERE slug = $1 AND status = 'active'",
        tenant_id,
    )

    if row is None:
        # Auto-create default tenant on first use
        await pool.execute(
            """
            INSERT INTO tenants (name, slug, config, plan, status)
            VALUES ($1, $2, $3, 'free', 'active')
            ON CONFLICT (slug) DO NOTHING
            """,
            f"Tenant {tenant_id}",
            tenant_id,
            json.dumps({}),
        )
        logger.info("db.tenant_created", tenant_id=tenant_id)
        config_data = {}
    else:
        config_data = json.loads(row["config"]) if isinstance(row["config"], str) else dict(row["config"])

    return TenantConfig(
        tenant_id=tenant_id,
        planner_model=config_data.get("planner_model", "gpt-4o-mini"),
        coder_model=config_data.get("coder_model", "gpt-4o"),
        reviewer_model=config_data.get("reviewer_model", "gpt-4o-mini"),
        score_threshold=float(config_data.get("score_threshold", 7.0)),
        max_iterations=int(config_data.get("max_iterations", 5)),
        token_budget=int(config_data.get("token_budget", 50000)),
        cost_limit_usd=float(config_data.get("cost_limit_usd", 1.00)),
        allowed_stacks=config_data.get(
            "allowed_stacks",
            ["Node.js", "Python", "React", "Next.js", "FastAPI", "Express"],
        ),
    )


# ---------------------------------------------------------------------------
# Execution persistence
# ---------------------------------------------------------------------------

async def save_execution_result(
    pool: asyncpg.Pool,
    tenant_id: str,
    execution_id: str,
    task_type: str,
    payload: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    """
    Persist execution outcome for audit, billing, and debugging.
    Resolves the tenant UUID from slug first.
    """
    try:
        tenant_uuid = await pool.fetchval(
            "SELECT id FROM tenants WHERE slug = $1", tenant_id
        )
        if tenant_uuid is None:
            logger.warning("db.save_execution.no_tenant", tenant_id=tenant_id)
            return

        await pool.execute(
            """
            INSERT INTO executions (
                tenant_id, execution_id, task_type, payload,
                success, result, error,
                tokens_input, tokens_output, compute_seconds, cost_usd,
                started_at, completed_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10, $11,
                NOW() - ($12 || ' seconds')::interval, NOW()
            )
            ON CONFLICT (execution_id) DO UPDATE SET
                success = EXCLUDED.success,
                result = EXCLUDED.result,
                error = EXCLUDED.error,
                tokens_input = EXCLUDED.tokens_input,
                tokens_output = EXCLUDED.tokens_output,
                cost_usd = EXCLUDED.cost_usd,
                completed_at = NOW()
            """,
            tenant_uuid,
            execution_id,
            task_type,
            json.dumps(payload),
            result.get("success", False),
            json.dumps(result),
            result.get("error"),
            result.get("tokens_used", 0) // 3,           # rough input split
            result.get("tokens_used", 0) * 2 // 3,       # rough output split
            float(result.get("duration_seconds", 0)),
            float(result.get("cost_usd", 0)),
            float(result.get("duration_seconds", 0)),
        )

        logger.info(
            "db.execution_saved",
            execution_id=execution_id,
            success=result.get("success"),
            cost_usd=result.get("cost_usd", 0),
        )

    except Exception as e:
        # Never let a DB write crash the main flow — log and continue
        logger.error("db.save_execution_failed", execution_id=execution_id, error=str(e))


# ---------------------------------------------------------------------------
# Sandbox state — persistent registry (fixes in-process dict leak)
# ---------------------------------------------------------------------------

async def register_sandbox(
    pool: asyncpg.Pool,
    container_name: str,
    execution_id: str,
    workspace: str,
) -> None:
    await pool.execute(
        """
        INSERT INTO sandbox_registry (container_name, execution_id, workspace, created_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (container_name) DO NOTHING
        """,
        container_name, execution_id, workspace,
    )


async def deregister_sandbox(pool: asyncpg.Pool, container_name: str) -> None:
    await pool.execute(
        "DELETE FROM sandbox_registry WHERE container_name = $1",
        container_name,
    )


async def get_expired_sandboxes(
    pool: asyncpg.Pool, max_age_minutes: int = 10
) -> List[Dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    rows = await pool.fetch(
        "SELECT container_name, workspace FROM sandbox_registry WHERE created_at < $1",
        cutoff,
    )
    return [dict(r) for r in rows]


async def load_all_sandboxes(pool: asyncpg.Pool) -> List[Dict[str, Any]]:
    """
    Called at startup to reconcile any containers that survived a crash.
    """
    rows = await pool.fetch(
        "SELECT container_name, execution_id, workspace, created_at FROM sandbox_registry"
    )
    return [dict(r) for r in rows]
