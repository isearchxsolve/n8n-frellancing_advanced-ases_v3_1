"""
ASES - Job Queue
Redis-backed async job queue using RQ.
API endpoints enqueue and return job_id immediately.
Worker processes pick up jobs and run the agent loop.
"""

import os
import json
import uuid
from typing import Any, Dict, Optional
from datetime import datetime

import structlog
from redis import Redis
from rq import Queue, Worker, Connection
from rq.job import Job

logger = structlog.get_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = "ases_jobs"
JOB_TTL = 86400        # 24 hours — keep results for billing/audit
JOB_TIMEOUT = 600      # 10 minutes per job max


def get_redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=False)


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_redis(), default_timeout=JOB_TIMEOUT)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue_agent_job(
    task_type: str,
    payload: Dict[str, Any],
    tenant_id: str,
    execution_id: str,
) -> str:
    """
    Push a job onto the queue. Returns the RQ job ID (== execution_id).
    The API returns this immediately; polling /jobs/{id} gives status.
    """
    q = get_queue()

    job = q.enqueue(
        "worker.execute_job",          # resolved by worker process
        args=(task_type, payload, tenant_id, execution_id),
        job_id=execution_id,           # use our own ID so clients can poll by it
        result_ttl=JOB_TTL,
        failure_ttl=JOB_TTL,
        retry=Retry(max=2, interval=[10, 30]),
    )

    logger.info(
        "queue.enqueued",
        execution_id=execution_id,
        task_type=task_type,
        tenant_id=tenant_id,
        queue_position=q.count,
    )

    return job.id


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------

def get_job_status(execution_id: str) -> Dict[str, Any]:
    """
    Returns current job status. Called by GET /jobs/{execution_id}.
    """
    try:
        job = Job.fetch(execution_id, connection=get_redis())
    except Exception:
        return {"status": "not_found", "execution_id": execution_id}

    status_map = {
        "queued":   "queued",
        "started":  "running",
        "finished": "complete",
        "failed":   "failed",
        "stopped":  "cancelled",
        "deferred": "queued",
    }

    result = {
        "execution_id": execution_id,
        "status": status_map.get(job.get_status().value, "unknown"),
        "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
    }

    if job.is_finished and job.result:
        result["result"] = job.result

    if job.is_failed:
        result["error"] = str(job.exc_info) if job.exc_info else "unknown error"

    return result


# ---------------------------------------------------------------------------
# Retry helper (imported at top-level call site)
# ---------------------------------------------------------------------------

from rq import Retry  # noqa: E402 — after Redis import to avoid circular
