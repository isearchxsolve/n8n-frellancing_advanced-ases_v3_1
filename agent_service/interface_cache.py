"""
ASES - Interface Signature Cache (Gap Fix: cross-job differ starts cold)
=========================================================================
Gives SemanticDiffer a warm start by persisting interface signatures across jobs.

Problem:
    SemanticDiffer.diff() builds its interface map from the files in the current job.
    On iteration 1, there is no previous baseline — the differ has no knowledge of
    what a file's exports looked like before the coder touched it.

    Example failure:
        Job 1 (passes): auth.js exports {login, logout, refreshToken}
        Job 2, iteration 1: coder generates auth.js exporting {login, logout}
                            → refreshToken is GONE before differ runs
                            → differ can't detect the regression (no prior baseline)
        The broken import in routes.js only surfaces as a cryptic TypeError.

Solution — interface_signatures table:
    On every successful job completion, store the interface map for each file
    keyed by (tenant_id, tech_stack, file_path_pattern).

    On differ init for a new job, load the cached signatures. The differ now
    starts with a baseline that reflects what successful jobs look like,
    and can detect regressions vs that baseline even on iteration 1.

    Cache key uses file_path_pattern (e.g. "auth.js", "routes/auth.js")
    rather than exact path, so it survives minor project structure changes.

SQL migration:
    See database/migration_vector_memory.sql (both migrations in one file)
"""

import json
import re
from typing import Dict, List, Optional, Set

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Store (called after successful job delivery)
# ---------------------------------------------------------------------------

async def store_interface_signatures(
    pool,
    tenant_uuid: str,
    tech_stack: str,
    files: List[Dict[str, str]],
    execution_id: str,
) -> None:
    """
    Persist the interface signatures of all files in a successful job.
    Uses UPSERT — newer successful jobs overwrite stale signatures.
    """
    from semantic_differ import _extract_interface

    for f in files:
        path = f["path"]
        content = f["content"]
        iface = _extract_interface(path, content)
        if iface is None or not iface.exports:
            continue

        pattern = _path_pattern(path)

        try:
            await pool.execute(
                """
                INSERT INTO interface_signatures
                    (tenant_id, tech_stack, file_pattern, exports, imports_from, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (tenant_id, tech_stack, file_pattern)
                DO UPDATE SET
                    exports     = EXCLUDED.exports,
                    imports_from = EXCLUDED.imports_from,
                    updated_at  = NOW(),
                    hit_count   = interface_signatures.hit_count + 1
                """,
                tenant_uuid,
                tech_stack,
                pattern,
                json.dumps(iface.exports),
                json.dumps({k: v for k, v in iface.imports.items()}),
            )
        except Exception as e:
            logger.warning(
                "interface_cache.store_failed",
                path=path,
                error=str(e),
                execution_id=execution_id,
            )

    logger.info(
        "interface_cache.stored",
        execution_id=execution_id,
        file_count=len(files),
    )


# ---------------------------------------------------------------------------
# Load (called at start of a new job to warm SemanticDiffer)
# ---------------------------------------------------------------------------

async def load_interface_signatures(
    pool,
    tenant_uuid: str,
    tech_stack: str,
) -> Dict[str, List[str]]:
    """
    Return a map of {file_pattern: [exports]} for use as a differ baseline.
    Returns {} if unavailable.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT file_pattern, exports
            FROM interface_signatures
            WHERE tenant_id = $1
              AND tech_stack = $2
            ORDER BY hit_count DESC
            LIMIT 100
            """,
            tenant_uuid,
            tech_stack,
        )
        result = {}
        for row in rows:
            try:
                exports = json.loads(row["exports"])
                result[row["file_pattern"]] = exports
            except Exception:
                pass

        logger.info(
            "interface_cache.loaded",
            tenant=str(tenant_uuid)[:8],
            tech_stack=tech_stack,
            patterns=len(result),
        )
        return result

    except Exception as e:
        logger.warning("interface_cache.load_failed", error=str(e))
        return {}


# ---------------------------------------------------------------------------
# SemanticDiffer warm-start wrapper
# ---------------------------------------------------------------------------

def build_warm_baseline(
    files: List[Dict[str, str]],
    cached_signatures: Dict[str, List[str]],
) -> List[Dict[str, str]]:
    """
    Constructs a synthetic "previous iteration" file list by merging:
    1. The actual current files (as provided by the job)
    2. Cached interface signatures for any files that match a known pattern

    This gives the differ a non-empty baseline on iteration 1, so regressions
    against the tenant's typical interface shape are caught immediately.

    Returns a list of synthetic files suitable for differ.diff(baseline, current).
    """
    if not cached_signatures:
        return []

    synthetic = []
    for f in files:
        pattern = _path_pattern(f["path"])
        cached_exports = cached_signatures.get(pattern)
        if cached_exports:
            # Construct a minimal synthetic file that "exports" the cached names
            synthetic_content = _synthesize_exports(f["path"], cached_exports)
            synthetic.append({"path": f["path"], "content": synthetic_content})

    return synthetic


def _path_pattern(path: str) -> str:
    """
    Normalise a file path to a pattern for cross-job matching.
    Strips leading directory components, keeps filename + one parent dir.

    Examples:
        "src/routes/auth.js"     → "routes/auth.js"
        "app/controllers/user.py" → "controllers/user.py"
        "index.js"               → "index.js"
    """
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1]


def _synthesize_exports(path: str, exports: List[str]) -> str:
    """
    Generate minimal synthetic file content that will produce the given
    export list when parsed by the differ's interface extractor.
    """
    if path.endswith(".py"):
        lines = [f"def {name}(): pass" for name in exports]
    else:
        # JS/TS: named exports
        lines = [f"export function {name}() {{}}" for name in exports]
    return "\n".join(lines)
