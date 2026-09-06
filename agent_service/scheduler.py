"""
ASES - Global Scheduler / Load Balancer
Resource-aware job routing with priority queues.

Priority tiers:
  P0 - critical   (internal health, billing hooks)
  P1 - high       (enterprise tenants, paid plans)
  P2 - normal     (pro tenants)
  P3 - low        (free tier, background tasks)

Workers pull from the highest non-empty queue first.
"""

import os
import json
import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import structlog
from redis import Redis
from rq import Queue
from rq import Retry

logger = structlog.get_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Queue names ordered by priority (workers drain P0 → P3)
PRIORITY_QUEUES = {
    "critical": "ases_p0_critical",
    "high":     "ases_p1_high",
    "normal":   "ases_p2_normal",
    "low":      "ases_p3_low",
}

# Plan → priority mapping
PLAN_PRIORITY = {
    "enterprise": "high",
    "pro":        "normal",
    "free":       "low",
}

JOB_TTL     = 86400   # 24 h
JOB_TIMEOUT = 600     # 10 min hard cap per job

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _queue(name: str) -> Queue:
    return Queue(name, connection=Redis.from_url(REDIS_URL, decode_responses=False),
                 default_timeout=JOB_TIMEOUT)


# ---------------------------------------------------------------------------
# Resource budgeting
# ---------------------------------------------------------------------------

_ACTIVE_KEY = "ases:scheduler:active_jobs"
_CPU_KEY    = "ases:scheduler:cpu_load"      # written by autoscaler heartbeat


def get_active_job_count() -> int:
    r = _redis()
    v = r.get(_ACTIVE_KEY)
    return int(v) if v else 0


def increment_active(execution_id: str) -> int:
    r = _redis()
    count = r.incr(_ACTIVE_KEY)
    r.setex(f"ases:scheduler:job:{execution_id}:start", JOB_TTL, int(time.time()))
    return count


def decrement_active(execution_id: str) -> int:
    r = _redis()
    count = r.decr(_ACTIVE_KEY)
    r.delete(f"ases:scheduler:job:{execution_id}:start")
    if count < 0:
        r.set(_ACTIVE_KEY, 0)
        count = 0
    return count


def record_cpu_load(load: float) -> None:
    """Written by the autoscaler probe; read by enqueue_with_priority."""
    _redis().setex(_CPU_KEY, 30, str(load))   # expires after 30 s


def get_cpu_load() -> float:
    v = _redis().get(_CPU_KEY)
    return float(v) if v else 0.0


# ---------------------------------------------------------------------------
# Priority resolution
# ---------------------------------------------------------------------------

def resolve_priority(
    tenant_plan: str,
    task_type: str,
    payload: Dict[str, Any],
) -> str:
    """
    Determine queue priority.
    Callers can pass explicit 'priority' in payload to override.
    """
    explicit = payload.get("priority")
    if explicit and explicit in PRIORITY_QUEUES:
        return explicit

    # Internal/system tasks are always critical
    if task_type in ("health_check", "billing_sync", "sandbox_gc"):
        return "critical"

    return PLAN_PRIORITY.get(tenant_plan, "normal")


# ---------------------------------------------------------------------------
# Enqueue with priority
# ---------------------------------------------------------------------------

def enqueue_with_priority(
    task_type: str,
    payload: Dict[str, Any],
    tenant_id: str,
    execution_id: str,
    tenant_plan: str = "free",
) -> Tuple[str, str]:
    """
    Route job to the appropriate priority queue.
    Returns (job_id, queue_name).
    """
    priority = resolve_priority(tenant_plan, task_type, payload)
    queue_name = PRIORITY_QUEUES[priority]

    q = _queue(queue_name)
    job = q.enqueue(
        "worker.execute_job",
        args=(task_type, payload, tenant_id, execution_id),
        job_id=execution_id,
        result_ttl=JOB_TTL,
        failure_ttl=JOB_TTL,
        retry=Retry(max=2, interval=[10, 30]),
        meta={
            "priority": priority,
            "tenant_plan": tenant_plan,
            "enqueued_at": time.time(),
        },
    )

    increment_active(execution_id)

    logger.info(
        "scheduler.enqueued",
        execution_id=execution_id,
        task_type=task_type,
        tenant_id=tenant_id,
        priority=priority,
        queue=queue_name,
        active_jobs=get_active_job_count(),
    )

    return job.id, queue_name


# ---------------------------------------------------------------------------
# Queue depth metrics (used by autoscaler)
# ---------------------------------------------------------------------------

def get_queue_depths() -> Dict[str, int]:
    depths = {}
    r = Redis.from_url(REDIS_URL, decode_responses=False)
    for label, name in PRIORITY_QUEUES.items():
        depths[label] = Queue(name, connection=r).count
    return depths


def get_worker_count() -> int:
    """Number of live RQ workers across all queues."""
    from rq import Worker
    r = Redis.from_url(REDIS_URL, decode_responses=False)
    return len(Worker.all(connection=r))


# ---------------------------------------------------------------------------
# Worker startup helper — bind to all priority queues
# ---------------------------------------------------------------------------

def start_priority_worker() -> None:
    """
    Launch a single RQ worker that drains queues in priority order.
    Call this from worker.py instead of the bare Worker(queues=[QUEUE_NAME]).
    """
    from redis import Redis as SyncRedis
    from rq import Worker, Connection

    conn = SyncRedis.from_url(REDIS_URL)
    queue_names = list(PRIORITY_QUEUES.values())   # ordered P0 → P3

    logger.info("scheduler.worker_starting", queues=queue_names)

    with Connection(conn):
        worker = Worker(
            queues=queue_names,
            connection=conn,
            log_job_description=True,
        )
        worker.work(with_scheduler=True)
