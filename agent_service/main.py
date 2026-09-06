"""
ASES - FastAPI Agent Service Entry Point
v2: endpoints enqueue jobs and return immediately; /jobs/{id} for polling.
"""

import os
import uuid
import secrets
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Header, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import structlog

from job_queue import enqueue_agent_job, get_job_status
from sandbox import cleanup_expired_sandboxes, reconcile_sandboxes_on_startup
from models import TenantConfig
from db import get_db_pool, get_tenant_config_from_db, close_db_pool, save_execution_result
from auth import require_auth, rotate_tenant_key
from config import settings

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Allowed origins — read from env; defaults to localhost only
# ---------------------------------------------------------------------------
_CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5678",
).split(",") if o.strip()]


# ---------------------------------------------------------------------------
# Per-tenant rate limiting
# ---------------------------------------------------------------------------
# Sliding-window counter in Redis.  Each tenant gets RATE_LIMIT_RPM requests
# per minute across all their workers.  The window resets every 60 seconds.
# Configure per-deployment via env; defaults are conservative for early SaaS.

_RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))   # requests per minute per tenant
_RATE_LIMIT_BURST = int(os.getenv("RATE_LIMIT_BURST", "5")) # extra burst allowance


def _rate_limit_key(tenant_id: str) -> str:
    from datetime import datetime
    # 60-second window keyed by minute boundary
    window = datetime.utcnow().strftime("%Y%m%d%H%M")
    return f"ases:ratelimit:{tenant_id}:{window}"


async def check_rate_limit(tenant_id: str) -> None:
    """
    Increment this tenant's request counter for the current 60s window.
    Raises HTTP 429 if the limit is exceeded.
    Called as a FastAPI dependency on all job-enqueue endpoints.
    """
    from redis import Redis
    try:
        r = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        key = _rate_limit_key(tenant_id)
        count = r.incr(key)
        if count == 1:
            r.expire(key, 60)   # set TTL on first increment only

        limit = _RATE_LIMIT_RPM + _RATE_LIMIT_BURST
        if count > limit:
            logger.warning(
                "rate_limit.exceeded",
                tenant_id=tenant_id,
                count=count,
                limit=limit,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {_RATE_LIMIT_RPM} requests/minute. Retry after the current window resets.",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception as e:
        # Redis unavailable — fail open with a warning rather than blocking all traffic
        logger.error("rate_limit.redis_error", tenant_id=tenant_id, error=str(e))


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class ProcessJobRequest(BaseModel):
    job_id: str
    title: str
    description: str
    link: str
    pub_date: Optional[str] = None
    tenant_id: str = "default"

class JobAccepted(BaseModel):
    accepted: bool = True
    execution_id: str
    status_url: str

class DevTaskRequest(BaseModel):
    action: str = Field(..., pattern="^(generate_code|test|scaffold|deploy)$")
    task: str
    tech_stack: Optional[str] = "Node.js + Express"
    requirements: Optional[str] = ""
    project_name: Optional[str] = None
    repo_id: Optional[int] = None
    branch: Optional[str] = "main"
    tenant_id: str = "default"
    max_iterations: int = 5
    token_budget: Optional[int] = 50000
    cost_limit_usd: Optional[float] = Field(
        default=None,
        description="Hard cost ceiling in USD. Job is aborted if exceeded mid-execution. "
                    "Defaults to token_budget * $0.00002 if not set."
    )

class JobStatusResponse(BaseModel):
    execution_id: str
    status: str            # queued | running | complete | failed | not_found
    enqueued_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class CRMWebhookRequest(BaseModel):
    action: str = Field(..., pattern="^(new_client|update_status|add_note|invoice_paid)$")
    tenant_id: str = "default"
    payload: Dict[str, Any]

class HealthResponse(BaseModel):
    status: str
    version: str


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("agent_service.startup", version="2.4.0")

    # Observability: Prometheus metrics endpoint + OTel tracing
    from observability import instrument_app
    instrument_app(app)

    # Reconcile any containers that survived a previous crash
    await reconcile_sandboxes_on_startup()

    # Background sandbox cleanup every 60s
    cleanup_task = asyncio.create_task(_sandbox_cleanup_loop())

    yield

    cleanup_task.cancel()
    await close_db_pool()
    logger.info("agent_service.shutdown")


async def _sandbox_cleanup_loop():
    while True:
        try:
            cleanup_expired_sandboxes(max_age_minutes=settings.SANDBOX_MAX_AGE_MINUTES)
        except Exception as e:
            logger.error("sandbox.cleanup_failed", error=str(e))
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ASES Agent Service",
    description="Autonomous Software Engineering System — v2",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "tenant-id", "x-tenant-id"],
)


async def require_auth_and_rate_limit(
    tenant_id: str = Depends(require_auth),
) -> str:
    """Single dependency that enforces auth then rate limiting in order."""
    await check_rate_limit(tenant_id)
    return tenant_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="healthy", version="2.0.0")


@app.get("/jobs/{execution_id}", response_model=JobStatusResponse)
async def job_status(execution_id: str):
    """Poll job status and result."""
    data = get_job_status(execution_id)
    return JobStatusResponse(**data)


@app.post("/process-job", response_model=JobAccepted, status_code=202)
async def process_job(
    request: ProcessJobRequest,
    tenant_id: str = Depends(require_auth_and_rate_limit),
):
    """
    Stage 1-2: Lead scoring and proposal generation.
    Returns 202 immediately; client polls /jobs/{execution_id}.
    Requires: x-tenant-id + x-api-key headers.
    """
    execution_id = str(uuid.uuid4())
    logger.info("process_job.enqueued", execution_id=execution_id, tenant_id=tenant_id)

    enqueue_agent_job(
        task_type="lead_pipeline",
        payload=request.model_dump(),
        tenant_id=tenant_id,
        execution_id=execution_id,
    )

    return JobAccepted(
        execution_id=execution_id,
        status_url=f"/jobs/{execution_id}",
    )


@app.post("/dev-task", response_model=JobAccepted, status_code=202)
async def dev_task(
    request: DevTaskRequest,
    tenant_id: str = Depends(require_auth_and_rate_limit),
):
    """
    Stage 6: Dev automation pipeline.
    Returns 202 immediately; client polls /jobs/{execution_id}.
    Requires: x-tenant-id + x-api-key headers.
    """
    execution_id = str(uuid.uuid4())
    logger.info(
        "dev_task.enqueued",
        execution_id=execution_id,
        tenant_id=tenant_id,
        action=request.action,
    )

    enqueue_agent_job(
        task_type=f"dev_{request.action}",
        payload=request.model_dump(),
        tenant_id=tenant_id,
        execution_id=execution_id,
    )

    return JobAccepted(
        execution_id=execution_id,
        status_url=f"/jobs/{execution_id}",
    )


@app.post("/crm-webhook", status_code=202)
async def crm_webhook(
    request: CRMWebhookRequest,
    tenant_id: str = Depends(require_auth_and_rate_limit),
):
    """
    Stage 5: CRM events — new client, status change, note, payment.
    Requires: x-tenant-id + x-api-key headers.
    """
    execution_id = str(uuid.uuid4())

    enqueue_agent_job(
        task_type=f"crm_{request.action}",
        payload=request.payload,
        tenant_id=tenant_id,
        execution_id=execution_id,
    )

    logger.info("crm_webhook.enqueued", execution_id=execution_id, action=request.action)
    return {"accepted": True, "execution_id": execution_id}


@app.post("/admin/tenants/{tenant_slug}/rotate-key")
async def admin_rotate_key(
    tenant_slug: str,
    admin_secret: str = Header(alias="x-admin-secret"),
):
    """
    Rotate the API key for a tenant. Admin-only.
    Guard this endpoint at the network/nginx level — do not expose publicly.
    Returns the new plaintext key ONCE; it is not stored and cannot be retrieved.
    """
    expected = os.getenv("ADMIN_SECRET")
    if not expected or not secrets.compare_digest(admin_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    new_key = await rotate_tenant_key(tenant_slug)
    return {"tenant_id": tenant_slug, "api_key": new_key, "warning": "Store this key now — it will not be shown again"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# ---------------------------------------------------------------------------
# v2.5 — Cold Outreach Personalization Endpoint
# ---------------------------------------------------------------------------

class PersonalizeEmailRequest(BaseModel):
    lead_id: str
    name: str
    company: Optional[str] = ""
    notes: Optional[str] = ""
    tenant_id: str = "default"


@app.post("/personalize-email", response_model=JobAccepted, status_code=202)
async def personalize_email(
    request: PersonalizeEmailRequest,
    tenant_id: str = Depends(require_auth_and_rate_limit),
):
    """
    v2.5: Cold outreach personalization.
    Generates a tailored cold email (subject + body) for a lead.
    Returns 202 immediately; poll /jobs/{execution_id} for result.
    Requires: x-tenant-id + x-api-key headers.
    """
    execution_id = str(uuid.uuid4())
    logger.info(
        "personalize_email.enqueued",
        execution_id=execution_id,
        tenant_id=tenant_id,
        lead_id=request.lead_id,
    )

    enqueue_agent_job(
        task_type="outreach_personalize",
        payload=request.model_dump(),
        tenant_id=tenant_id,
        execution_id=execution_id,
    )

    return JobAccepted(
        execution_id=execution_id,
        status_url=f"/jobs/{execution_id}",
    )
