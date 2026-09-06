"""
ASES - Docker Sandbox Execution
Isolated per-request containers for real code execution.

Key change from v1: sandbox state is persisted in Postgres (sandbox_registry),
not an in-process dict. This means state survives worker restarts and
container lists stay consistent across horizontally-scaled workers.
"""

import os
import subprocess
import shutil
import asyncio
from typing import Dict, Any, Optional, List

import structlog

from db import (
    get_db_pool,
    register_sandbox,
    deregister_sandbox,
    get_expired_sandboxes,
    load_all_sandboxes,
)

logger = structlog.get_logger()

SANDBOX_BASE_DIR = os.getenv("SANDBOX_BASE_DIR", "/tmp/ases-sandboxes")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Maximum Docker containers running simultaneously across all workers.
# Tune this to (host_memory_gb * 1000) / 512 — each container gets 512MB.
MAX_CONCURRENT_SANDBOXES = int(os.getenv("MAX_CONCURRENT_SANDBOXES", "10"))
_SANDBOX_COUNTER_KEY = "ases:active_sandboxes"

# Stack -> Docker image mapping
STACK_IMAGES = {
    "node.js":   "node:18-alpine",
    "nodejs":    "node:18-alpine",
    "express":   "node:18-alpine",
    "react":     "node:18-alpine",
    "next.js":   "node:18-alpine",
    "python":    "python:3.12-slim",
    "fastapi":   "python:3.12-slim",
    "flask":     "python:3.12-slim",
    "django":    "python:3.12-slim",
    "go":        "golang:1.22-alpine",
    "rust":      "rust:1.78-alpine",
    "default":   "node:18-alpine",
}

# Stack -> install + test command
STACK_TEST_COMMANDS = {
    "node.js":   "npm install --prefer-offline 2>&1 && npm test 2>&1",
    "nodejs":    "npm install --prefer-offline 2>&1 && npm test 2>&1",
    "express":   "npm install --prefer-offline 2>&1 && npm test 2>&1",
    "react":     "npm install --prefer-offline 2>&1 && npm test -- --watchAll=false 2>&1",
    "next.js":   "npm install --prefer-offline 2>&1 && npm run build 2>&1",
    "python":    "pip install -r requirements.txt -q 2>&1 && python -m pytest -q 2>&1",
    "fastapi":   "pip install -r requirements.txt -q 2>&1 && python -m pytest -q 2>&1",
    "flask":     "pip install -r requirements.txt -q 2>&1 && python -m pytest -q 2>&1",
    "django":    "pip install -r requirements.txt -q 2>&1 && python manage.py test 2>&1",
    "go":        "go test ./... 2>&1",
    "rust":      "cargo test 2>&1",
    "default":   "npm install --prefer-offline 2>&1 && npm test 2>&1",
}


def _resolve_stack(tech_stack: str) -> str:
    return tech_stack.lower().split("+")[0].strip()


def _get_image(tech_stack: str) -> str:
    key = _resolve_stack(tech_stack)
    return STACK_IMAGES.get(key, STACK_IMAGES["default"])


def get_test_command(tech_stack: str) -> str:
    key = _resolve_stack(tech_stack)
    return STACK_TEST_COMMANDS.get(key, STACK_TEST_COMMANDS["default"])


# ---------------------------------------------------------------------------
# Redis-based concurrency counter
# ---------------------------------------------------------------------------

def _get_redis():
    """Lazy import so Redis isn't required in unit-test contexts."""
    from redis import Redis
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _increment_sandbox_counter() -> int:
    """
    Atomically increment the active-sandbox counter.
    Returns the new value so the caller can check against the cap.
    """
    r = _get_redis()
    return r.incr(_SANDBOX_COUNTER_KEY)


def _decrement_sandbox_counter() -> None:
    r = _get_redis()
    # Never go below 0 (safety guard for counter drift after crashes)
    current = r.decr(_SANDBOX_COUNTER_KEY)
    if current < 0:
        r.set(_SANDBOX_COUNTER_KEY, 0)


# ---------------------------------------------------------------------------
# Sandbox creation (now async — called from async worker path)
# ---------------------------------------------------------------------------

async def create_sandbox(execution_id: str, tech_stack: str = "node.js") -> str:
    """
    Create an isolated Docker sandbox and register it in Postgres + Redis.

    This is now an async function so it can await DB registration directly,
    avoiding the deprecated asyncio.get_event_loop() pattern from v2.0.

    Raises RuntimeError if the concurrency cap is reached or Docker fails.
    """
    # 1. Concurrency cap check (atomic Redis increment)
    count = _increment_sandbox_counter()
    if count > MAX_CONCURRENT_SANDBOXES:
        _decrement_sandbox_counter()
        raise RuntimeError(
            f"Concurrency cap reached ({MAX_CONCURRENT_SANDBOXES} active sandboxes). "
            "Retry after a running job completes."
        )

    container_name = f"ases-{execution_id[:12]}"
    workspace = f"{SANDBOX_BASE_DIR}/{execution_id}"
    os.makedirs(workspace, exist_ok=True)
    image = _get_image(tech_stack)

    # 2. Spawn container (blocking subprocess — acceptable; it's fast)
    result = subprocess.run(
        [
            "docker", "run", "-dit",
            "--name", container_name,
            "--rm",
            "--cpus", "1.0",
            "--memory", "512m",
            "--pids-limit", "128",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "--security-opt", "no-new-privileges",
            "-v", f"{workspace}:/workspace",
            "-w", "/workspace",
            image,
            "sh", "-c", "while true; do sleep 1; done",
        ],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode != 0:
        # Container failed to start — release the counter slot immediately
        _decrement_sandbox_counter()
        raise RuntimeError(f"Failed to create sandbox: {result.stderr.strip()}")

    # 3. Register in Postgres (direct await — no event loop gymnastics)
    try:
        pool = await get_db_pool()
        await register_sandbox(pool, container_name, execution_id, workspace)
    except Exception as e:
        # Registration failure is non-fatal for the job but must be logged —
        # the sandbox will be reconciled at next startup or TTL cleanup.
        logger.error("sandbox.register_failed", container=container_name, error=str(e))

    logger.info("sandbox.created", container=container_name, image=image, execution_id=execution_id)
    return container_name


def _find_workspace(container_name: str) -> str:
    prefix = container_name.replace("ases-", "")
    base = SANDBOX_BASE_DIR
    if os.path.exists(base):
        for d in os.listdir(base):
            if d.startswith(prefix):
                return os.path.join(base, d)
    raise ValueError(f"Sandbox workspace not found for container {container_name}")


def write_file(container_name: str, path: str, content: str):
    workspace = _find_workspace(container_name)
    safe_path = path.lstrip("/")
    if ".." in safe_path:
        raise ValueError(f"Path traversal detected: {path}")
    full_path = os.path.join(workspace, safe_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.debug("sandbox.file_written", container=container_name, path=safe_path)


def run_command(container_name: str, command: str, timeout: int = 120) -> Dict[str, Any]:
    logger.info("sandbox.exec", container=container_name, cmd=command[:120])
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("sandbox.exec.timeout", container=container_name)
        return {"success": False, "stdout": "", "stderr": f"Timed out after {timeout}s", "returncode": -1}

    success = result.returncode == 0
    logger.info("sandbox.exec.complete", container=container_name, success=success)
    return {
        "success": success,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-4000:],
        "returncode": result.returncode,
    }


async def cleanup_sandbox(container_name: str) -> None:
    """
    Stop the container, remove workspace files, deregister from DB,
    and release the Redis concurrency slot.

    Now async so it can be awaited directly from the worker.
    The --rm flag on docker run means Docker removes the container
    automatically on stop; this function handles our own bookkeeping.
    """
    # Stop the container (--rm flag handles actual removal)
    try:
        subprocess.run(["docker", "stop", "-t", "5", container_name], capture_output=True, timeout=15)
    except Exception as e:
        logger.warning("sandbox.stop_failed", container=container_name, error=str(e))

    # Remove workspace
    try:
        workspace = _find_workspace(container_name)
        shutil.rmtree(workspace, ignore_errors=True)
    except Exception:
        pass

    # Deregister from Postgres
    try:
        pool = await get_db_pool()
        await deregister_sandbox(pool, container_name)
    except Exception as e:
        logger.error("sandbox.deregister_failed", container=container_name, error=str(e))

    # Release the concurrency slot
    _decrement_sandbox_counter()

    logger.info("sandbox.cleaned", container=container_name)


async def cleanup_expired_sandboxes(max_age_minutes: int = 10) -> None:
    """Called by the background loop in main.py every 60s."""
    try:
        pool = await get_db_pool()
        expired = await get_expired_sandboxes(pool, max_age_minutes)
        for s in expired:
            await cleanup_sandbox(s["container_name"])
        if expired:
            logger.info("sandbox.expired_cleaned", count=len(expired))
    except Exception as e:
        logger.error("sandbox.cleanup_async_failed", error=str(e))


async def reconcile_sandboxes_on_startup():
    try:
        pool = await get_db_pool()
        sandboxes = await load_all_sandboxes(pool)
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        running = set(result.stdout.strip().split("\n"))

        # Clean ghost DB records (containers that died while we were down)
        for s in sandboxes:
            if s["container_name"] not in running:
                logger.info("sandbox.reconcile.ghost", container=s["container_name"])
                await deregister_sandbox(pool, s["container_name"])
                shutil.rmtree(s.get("workspace", ""), ignore_errors=True)

        # Count how many ASES containers are actually running right now
        # and hard-set the Redis counter to that number.  This corrects
        # any drift caused by workers crashing mid-job.
        live_ases_count = sum(1 for name in running if name.startswith("ases-"))
        r = _get_redis()
        r.set(_SANDBOX_COUNTER_KEY, live_ases_count)

        logger.info(
            "sandbox.reconcile_complete",
            found=len(sandboxes),
            live_containers=live_ases_count,
            counter_reset_to=live_ases_count,
        )
    except Exception as e:
        logger.error("sandbox.reconcile_failed", error=str(e))


def commit_to_github(
    sandbox_id: str, project_name: str, files: list,
    github_token: Optional[str] = None,
) -> Optional[str]:
    try:
        workspace = _find_workspace(sandbox_id)
    except ValueError:
        return None

    token = github_token or os.getenv("GITHUB_TOKEN")
    if not token:
        logger.warning("github.no_token")
        return None

    safe_name = "".join(c if c.isalnum() or c in "-_." else "-" for c in project_name)[:100]

    # Write a throwaway credential helper script so the token never
    # appears in the git remote URL or .git/config.  The script is
    # chmod 700 and lives only for the duration of this function.
    askpass_path = os.path.join(workspace, ".git_askpass")
    with open(askpass_path, "w") as f:
        f.write(f"#!/bin/sh\necho '{token}'\n")
    os.chmod(askpass_path, 0o700)

    # Environment for all git push calls — no token in any argument
    git_env = {
        **os.environ,
        "GIT_ASKPASS": askpass_path,
        "GIT_TERMINAL_PROMPT": "0",   # abort instead of hanging on prompt
    }

    try:
        for cmd in [
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "ases@automation.local"],
            ["git", "config", "user.name", "ASES Bot"],
            ["git", "add", "."],
            ["git", "commit", "-m", f"feat: initial commit — {safe_name}"],
        ]:
            subprocess.run(cmd, cwd=workspace, check=True, capture_output=True)

        import httpx
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
                json={"name": safe_name, "private": True, "auto_init": False},
            )
            if resp.status_code not in (201, 422):
                logger.error("github.repo.create_failed", status=resp.status_code)
                return None

            repo_data = resp.json()
            # Use plain https:// URL — credentials supplied via GIT_ASKPASS only
            clone_url = repo_data.get("clone_url", "")
            subprocess.run(
                ["git", "remote", "add", "origin", clone_url],
                cwd=workspace, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=workspace, check=True, capture_output=True,
                env=git_env,
            )
            repo_url = repo_data.get("html_url")
            logger.info("github.push.success", repo=repo_url)
            return repo_url
    except Exception as e:
        logger.error("github.commit_failed", error=str(e))
        return None
    finally:
        # Always remove the askpass script — even if push failed
        try:
            os.remove(askpass_path)
        except OSError:
            pass
