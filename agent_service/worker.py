"""
ASES - RQ Worker
Run with: python worker.py
Or via docker-compose: command: python worker.py

One worker process handles one job at a time (safe for Docker-in-Docker sandbox).
Scale horizontally: docker-compose up --scale worker=4
"""

import os
import sys
import asyncio
import structlog

from redis import Redis
from rq import Worker, Queue, Connection

from agent_loop import run_multi_agent
from models import TenantConfig
from db import get_db_pool, get_tenant_config_from_db, save_execution_result

logger = structlog.get_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = "ases_jobs"


# ---------------------------------------------------------------------------
# Job handler — called by RQ worker
# ---------------------------------------------------------------------------

def execute_job(
    task_type: str,
    payload: dict,
    tenant_id: str,
    execution_id: str,
) -> dict:
    """
    Synchronous wrapper so RQ can call our async agent loop.
    Fetches tenant config from DB, runs the pipeline, persists results.
    """
    logger.info(
        "worker.job_start",
        execution_id=execution_id,
        task_type=task_type,
        tenant_id=tenant_id,
    )

    # Run async loop in a fresh event loop (RQ workers are sync)
    result = asyncio.run(_run(task_type, payload, tenant_id, execution_id))

    logger.info(
        "worker.job_complete",
        execution_id=execution_id,
        success=result.get("success"),
        tokens=result.get("tokens_used", 0),
        cost=result.get("cost_usd", 0),
    )

    return result


async def _run(
    task_type: str,
    payload: dict,
    tenant_id: str,
    execution_id: str,
) -> dict:
    pool = await get_db_pool()
    config = await get_tenant_config_from_db(pool, tenant_id)

    if task_type.startswith("crm_"):
        action = task_type.replace("crm_", "")
        result = await _handle_crm(pool, action, payload, tenant_id, execution_id)
    elif task_type == "outreach_personalize":
        # Routed directly through run_multi_agent — no special handling needed
        result = await run_multi_agent(task_type, payload, config, execution_id)
    else:
        try:
            result = await run_multi_agent(task_type, payload, config, execution_id)
        except Exception as e:
            error_result = {
                "success": False, "error": str(e),
                "tokens_used": 0, "cost_usd": 0.0, "iterations": 0, "logs": str(e),
            }
            await save_execution_result(pool, tenant_id, execution_id, task_type, payload, error_result)
            raise

    await save_execution_result(pool, tenant_id, execution_id, task_type, payload, result)
    return result


async def _handle_crm(pool, action: str, payload: dict, tenant_id: str, execution_id: str) -> dict:
    """
    CRM event handlers — previously stubs, now wired to Postgres.
    """
    import json
    logger.info("crm.processing", action=action, tenant_id=tenant_id, execution_id=execution_id)

    try:
        tenant_uuid = await pool.fetchval("SELECT id FROM tenants WHERE slug = $1", tenant_id)
        if not tenant_uuid:
            return {"success": False, "error": f"Tenant '{tenant_id}' not found"}

        if action == "new_client":
            await pool.execute(
                """
                INSERT INTO clients (tenant_id, client_id, name, email, company, project_type, budget, status, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'lead', $8)
                ON CONFLICT (tenant_id, client_id) DO UPDATE SET
                    name = EXCLUDED.name, email = EXCLUDED.email,
                    company = EXCLUDED.company, updated_at = NOW()
                """,
                tenant_uuid,
                payload.get("client_id", execution_id),
                payload.get("name", "Unknown"),
                payload.get("email"),
                payload.get("company"),
                payload.get("project_type"),
                payload.get("budget"),
                payload.get("source", "manual"),
            )

        elif action == "update_status":
            rows = await pool.execute(
                """
                UPDATE clients SET status = $1, updated_at = NOW()
                WHERE tenant_id = $2 AND client_id = $3
                """,
                payload.get("status", "lead"),
                tenant_uuid,
                payload.get("client_id"),
            )
            if rows == "UPDATE 0":
                return {"success": False, "error": "Client not found"}

        elif action == "add_note":
            client_uuid = await pool.fetchval(
                "SELECT id FROM clients WHERE tenant_id = $1 AND client_id = $2",
                tenant_uuid, payload.get("client_id"),
            )
            if not client_uuid:
                return {"success": False, "error": "Client not found"}
            await pool.execute(
                "INSERT INTO client_notes (tenant_id, client_id, note, author) VALUES ($1, $2, $3, $4)",
                tenant_uuid, client_uuid,
                payload.get("note", ""),
                payload.get("author", "system"),
            )

        elif action == "invoice_paid":
            client_uuid = await pool.fetchval(
                "SELECT id FROM clients WHERE tenant_id = $1 AND client_id = $2",
                tenant_uuid, payload.get("client_id"),
            )
            if not client_uuid:
                return {"success": False, "error": "Client not found"}
            await pool.execute(
                """
                INSERT INTO payments (tenant_id, client_id, amount, currency, invoice_id, method, paid_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                """,
                tenant_uuid, client_uuid,
                float(payload.get("amount", 0)),
                payload.get("currency", "USD"),
                payload.get("invoice_id"),
                payload.get("method"),
            )
            # Update client status to active on payment
            await pool.execute(
                "UPDATE clients SET status = 'active', updated_at = NOW() WHERE id = $1",
                client_uuid,
            )

        logger.info("crm.complete", action=action, execution_id=execution_id)
        return {"success": True, "action": action}

    except Exception as e:
        logger.error("crm.failed", action=action, error=str(e))
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Worker entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Use the priority scheduler — drains P0→P3 queues in order
    from scheduler import start_priority_worker, decrement_active
    logger.info("worker.starting", mode="priority_scheduler", redis=REDIS_URL)
    start_priority_worker()
