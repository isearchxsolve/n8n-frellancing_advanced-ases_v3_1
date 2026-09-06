"""
ASES - Authentication Middleware
---------------------------------
Every mutating endpoint requires two headers:

    x-tenant-id: <slug>          # which tenant
    x-api-key:   <plaintext key> # proves ownership

The plaintext key is never stored. We hash it with SHA-256 and compare
against the api_key_hash column in the tenants table.

Key lifecycle
-------------
On first use (auto-created tenant), api_key_hash is NULL.  The system
generates a fresh key, prints it once to the log, and stores only the
hash.  The operator is responsible for capturing it from the logs and
delivering it to the tenant.  There is no retrieval endpoint — if a
key is lost, rotate it via the /admin/tenants/{slug}/rotate-key route
(not exposed publicly; add your own admin guard there).

Why SHA-256 and not bcrypt?
---------------------------
API keys are high-entropy random strings (~32 bytes), so bcrypt's
purpose (slowing brute-force of low-entropy passwords) doesn't apply.
SHA-256 is fast enough for per-request lookup and avoids the latency
cost of bcrypt on the hot path.
"""

import hashlib
import os
import secrets
from typing import Optional

import structlog
from fastapi import Header, HTTPException, status

from db import get_db_pool

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_key(plaintext: str) -> str:
    """SHA-256 hex digest of a plaintext API key."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _generate_key() -> str:
    """Cryptographically random 32-byte URL-safe key (43 chars, no padding)."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

async def _get_tenant_key_hash(pool, tenant_id: str) -> Optional[str]:
    """Return the stored api_key_hash for a tenant slug, or None."""
    return await pool.fetchval(
        "SELECT api_key_hash FROM tenants WHERE slug = $1 AND status = 'active'",
        tenant_id,
    )


async def _set_tenant_key_hash(pool, tenant_id: str, key_hash: str) -> None:
    await pool.execute(
        "UPDATE tenants SET api_key_hash = $1 WHERE slug = $2",
        key_hash,
        tenant_id,
    )


# ---------------------------------------------------------------------------
# Bootstrap: assign a key to a newly created (keyless) tenant
# ---------------------------------------------------------------------------

async def bootstrap_tenant_key(pool, tenant_id: str) -> str:
    """
    Called once when a tenant has no key yet.
    Generates a key, stores the hash, logs the plaintext ONCE.
    Returns the plaintext key so the caller can surface it.
    """
    key = _generate_key()
    key_hash = _hash_key(key)
    await _set_tenant_key_hash(pool, tenant_id, key_hash)

    # Log at WARNING so it appears even in quiet logging configs.
    # The operator must capture this — it is never retrievable again.
    logger.warning(
        "auth.key_generated",
        tenant_id=tenant_id,
        api_key=key,
        message="CAPTURE THIS KEY — it will not be shown again",
    )
    return key


# ---------------------------------------------------------------------------
# FastAPI dependency — drop this into any route that needs auth
# ---------------------------------------------------------------------------

async def require_auth(
    x_tenant_id: str = Header(default="default", alias="x-tenant-id"),
    x_api_key: str = Header(default="", alias="x-api-key"),
) -> str:
    """
    FastAPI dependency.  Returns the verified tenant_id on success.
    Raises HTTP 401 on any failure (wrong key, missing key, unknown tenant).

    Usage:
        @app.post("/process-job")
        async def process_job(
            request: ProcessJobRequest,
            tenant_id: str = Depends(require_auth),
        ): ...
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing x-api-key header",
        )

    pool = await get_db_pool()

    stored_hash = await _get_tenant_key_hash(pool, x_tenant_id)

    if stored_hash is None:
        # Tenant exists (auto-created) but has no key yet — bootstrap it.
        # Only happens on the very first authenticated call per tenant.
        await bootstrap_tenant_key(pool, x_tenant_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Tenant key just initialised — check service logs for the "
                "plaintext key and retry with x-api-key set."
            ),
        )

    incoming_hash = _hash_key(x_api_key)

    # Constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(incoming_hash, stored_hash):
        logger.warning(
            "auth.failed",
            tenant_id=x_tenant_id,
            reason="key_mismatch",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    logger.debug("auth.ok", tenant_id=x_tenant_id)
    return x_tenant_id


# ---------------------------------------------------------------------------
# Key rotation (call from an admin-guarded route)
# ---------------------------------------------------------------------------

async def rotate_tenant_key(tenant_id: str) -> str:
    """
    Invalidates the current key and issues a new one.
    Returns plaintext new key — log/return to operator once.
    """
    pool = await get_db_pool()
    key = _generate_key()
    key_hash = _hash_key(key)
    await _set_tenant_key_hash(pool, tenant_id, key_hash)
    logger.warning(
        "auth.key_rotated",
        tenant_id=tenant_id,
        api_key=key,
        message="CAPTURE THIS KEY — it will not be shown again",
    )
    return key
