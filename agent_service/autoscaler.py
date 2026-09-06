"""
ASES - Autoscaler
Dynamically scales RQ workers in response to queue depth and CPU load.

Strategy:
  - Poll queue depths every POLL_INTERVAL seconds
  - Scale UP  if (total_queued / workers) > SCALE_UP_THRESHOLD
  - Scale DOWN if workers are idle and queues are empty for SCALE_DOWN_GRACE
  - Respect MIN_WORKERS / MAX_WORKERS hard limits
  - CPU guard: never scale UP when host CPU > CPU_SCALE_UP_MAX

Deployment note:
  Run ONE autoscaler process per cluster (not per worker).
  docker-compose: `command: python autoscaler.py`
"""

import os
import sys
import time
import asyncio
import subprocess
import signal
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configuration (all overridable via env)
# ---------------------------------------------------------------------------

POLL_INTERVAL       = int(os.getenv("AUTOSCALER_POLL_INTERVAL", "15"))      # seconds
MIN_WORKERS         = int(os.getenv("AUTOSCALER_MIN_WORKERS", "1"))
MAX_WORKERS         = int(os.getenv("AUTOSCALER_MAX_WORKERS", "8"))
SCALE_UP_THRESHOLD  = float(os.getenv("AUTOSCALER_SCALE_UP_THRESHOLD", "3.0"))   # jobs/worker
SCALE_DOWN_GRACE    = int(os.getenv("AUTOSCALER_SCALE_DOWN_GRACE", "60"))    # seconds idle → scale down
CPU_SCALE_UP_MAX    = float(os.getenv("AUTOSCALER_CPU_MAX", "80.0"))         # % — don't add workers above this
WORKER_CMD          = os.getenv("AUTOSCALER_WORKER_CMD", "python worker.py")

# Track when we last saw work (to enforce scale-down grace period)
_last_work_seen: float = time.time()
_managed_procs: list[subprocess.Popen] = []


# ---------------------------------------------------------------------------
# CPU probe
# ---------------------------------------------------------------------------

def _cpu_percent() -> float:
    """Read 1-second CPU load. Falls back to 0 if psutil absent."""
    try:
        import psutil
        pct = psutil.cpu_percent(interval=1)
        # Publish to Redis so scheduler.py can read it
        from scheduler import record_cpu_load
        record_cpu_load(pct)
        return pct
    except ImportError:
        return 0.0


# ---------------------------------------------------------------------------
# Worker process management
# ---------------------------------------------------------------------------

def _live_workers() -> int:
    """Count worker subprocesses that are still running."""
    global _managed_procs
    _managed_procs = [p for p in _managed_procs if p.poll() is None]
    return len(_managed_procs)


def _spawn_worker() -> None:
    logger.info("autoscaler.spawn_worker")
    proc = subprocess.Popen(
        WORKER_CMD.split(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _managed_procs.append(proc)


def _kill_one_worker() -> None:
    """Gracefully terminate the most recently spawned extra worker."""
    for proc in reversed(_managed_procs):
        if proc.poll() is None:
            logger.info("autoscaler.kill_worker", pid=proc.pid)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _managed_procs.remove(proc)
            return


# ---------------------------------------------------------------------------
# Scaling decision
# ---------------------------------------------------------------------------

def _decide(total_queued: int, workers: int, cpu: float) -> str:
    """
    Returns: 'up' | 'down' | 'hold'
    """
    global _last_work_seen

    if total_queued > 0:
        _last_work_seen = time.time()

    ratio = total_queued / max(workers, 1)

    if ratio > SCALE_UP_THRESHOLD:
        if workers >= MAX_WORKERS:
            return "hold"
        if cpu > CPU_SCALE_UP_MAX:
            logger.warning(
                "autoscaler.cpu_guard",
                cpu=cpu,
                limit=CPU_SCALE_UP_MAX,
                message="Would scale up but CPU too high",
            )
            return "hold"
        return "up"

    idle_seconds = time.time() - _last_work_seen
    if workers > MIN_WORKERS and idle_seconds > SCALE_DOWN_GRACE and total_queued == 0:
        return "down"

    return "hold"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    from scheduler import get_queue_depths

    logger.info(
        "autoscaler.starting",
        min_workers=MIN_WORKERS,
        max_workers=MAX_WORKERS,
        poll_interval=POLL_INTERVAL,
    )

    # Ensure minimum workers are up at start
    while _live_workers() < MIN_WORKERS:
        _spawn_worker()

    while True:
        try:
            depths = get_queue_depths()
            total_queued = sum(depths.values())
            workers = _live_workers()
            cpu = _cpu_percent()

            decision = _decide(total_queued, workers, cpu)

            logger.info(
                "autoscaler.tick",
                total_queued=total_queued,
                workers=workers,
                cpu=f"{cpu:.1f}%",
                decision=decision,
                depths=depths,
            )

            if decision == "up":
                _spawn_worker()
            elif decision == "down":
                _kill_one_worker()

            # Ensure minimum is always maintained (e.g. after crash)
            while _live_workers() < MIN_WORKERS:
                logger.warning("autoscaler.respawn_minimum")
                _spawn_worker()

        except Exception as e:
            logger.error("autoscaler.error", error=str(e))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
