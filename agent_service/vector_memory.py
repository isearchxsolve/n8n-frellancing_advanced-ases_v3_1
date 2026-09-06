"""
ASES - Vector Memory (Gap Fix: keyword memory -> pgvector)
===========================================================
Replaces the ILIKE keyword match in retrieve_memory_patterns / store_memory_pattern
with pgvector cosine-similarity search.

Problem with ILIKE approach:
    retrieve_memory_patterns uses substring matching:
        WHERE context ILIKE '%jwt%' OR context ILIKE '%auth%'
    This fails completely on paraphrase:
        Stored: "implement authentication with tokens"
        Query:  "build a JWT auth service"
        Result: zero hits — same concept, no lexical overlap.

Solution:
    1. On store: embed the task description + solution summary -> float[1536] vector
       stored in code_patterns.embedding (pgvector column).
    2. On retrieve: embed the incoming task, run cosine similarity query:
       ORDER BY embedding <=> query_vec LIMIT 3
    3. Falls back to ILIKE silently if pgvector/openai unavailable (zero regression).

v2.6 additions:
- Design spec storage/retrieval (design_specs table)
- Unified embedding interface (_embed) used by both code patterns and design specs

SQL migration (run once):
    See database/migration_vector_memory.sql

Requirements added to requirements.txt:
    pgvector>=0.2.5    (Python client)
    openai>=1.30.0     (already present — used for embeddings)
"""

import json
import os
from typing import Optional, List

import structlog

logger = structlog.get_logger()

EMBEDDING_MODEL = "text-embedding-3-small"   # 1536 dims, cheap (~$0.00002/1k tokens)
SIMILARITY_THRESHOLD = 0.70                  # cosine similarity floor (0-1)
TOP_K = 3


# ---------------------------------------------------------------------------
# Embedding (shared interface)
# ---------------------------------------------------------------------------

async def _embed(text: str) -> Optional[List[float]]:
    """Return a 1536-dim embedding vector, or None on failure."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=api_key)
        resp = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],   # stay within token limit
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.warning("vector_memory.embed_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Code Pattern Store/Retrieve (existing functionality)
# ---------------------------------------------------------------------------

async def store_memory_pattern_vector(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    files: list,
    execution_id: str,
) -> None:
    """
    Store a successful solution with its embedding.
    Upserts: if the same (tenant, task hash) exists, increments success_count.
    """
    # Build compact solution summary for embedding
    entry_files = [f for f in files if any(
        k in f["path"] for k in ["index", "main", "app", "route", "controller"]
    )][:3] or files[:2]

    solution_text = "\n".join(
        f"FILE: {f['path']}\n{f['content'][:400]}" for f in entry_files
    )
    context_text = f"Task: {task}\nStack: {tech_stack}"
    embed_input = f"{context_text}\n\nSolution:\n{solution_text}"

    embedding = await _embed(embed_input)

    try:
        if embedding is not None:
            # pgvector path — store with embedding
            await pool.execute(
                """
                INSERT INTO code_patterns
                    (tenant_id, context, solution, pattern_type, tech_stack, embedding, success_count)
                VALUES ($1, $2, $3, 'success', $4, $5::vector, 1)
                ON CONFLICT (tenant_id, context_hash)
                DO UPDATE SET
                    success_count = code_patterns.success_count + 1,
                    embedding     = EXCLUDED.embedding,
                    updated_at    = NOW()
                """,
                tenant_uuid,
                context_text,
                solution_text[:1000],
                tech_stack,
                json.dumps(embedding),
            )
        else:
            # Fallback: store without embedding (ILIKE still works for retrieval)
            await pool.execute(
                """
                INSERT INTO code_patterns
                    (tenant_id, context, solution, pattern_type, tech_stack, success_count)
                VALUES ($1, $2, $3, 'success', $4, 1)
                ON CONFLICT (tenant_id, context_hash)
                DO UPDATE SET success_count = code_patterns.success_count + 1
                """,
                tenant_uuid,
                context_text,
                solution_text[:1000],
                tech_stack,
            )
        logger.info(
            "vector_memory.stored",
            execution_id=execution_id,
            has_embedding=embedding is not None,
        )
    except Exception as e:
        logger.warning("vector_memory.store_failed", error=str(e), execution_id=execution_id)


async def retrieve_memory_patterns_vector(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    execution_id: str,
) -> str:
    """
    Retrieve relevant past solutions using pgvector cosine similarity.
    Falls back to ILIKE keyword search if embedding unavailable.
    Returns a formatted string for injection into coder requirements, or "".
    """
    query_embedding = await _embed(f"Task: {task}\nStack: {tech_stack}")

    rows = []
    if query_embedding is not None:
        try:
            rows = await pool.fetch(
                """
                SELECT context, solution, success_count, tech_stack,
                       1 - (embedding <=> $2::vector) AS similarity
                FROM code_patterns
                WHERE tenant_id = $1
                  AND pattern_type = 'success'
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $2::vector) >= $3
                ORDER BY similarity DESC
                LIMIT $4
                """,
                tenant_uuid,
                json.dumps(query_embedding),
                SIMILARITY_THRESHOLD,
                TOP_K,
            )
            logger.info(
                "vector_memory.vector_search",
                execution_id=execution_id,
                hits=len(rows),
            )
        except Exception as e:
            logger.warning("vector_memory.vector_search_failed", error=str(e))
            rows = []

    # Fallback to ILIKE if vector search returned nothing or unavailable
    if not rows:
        try:
            keywords = [w.lower() for w in task.split() if len(w) > 4][:5]
            if keywords:
                like_conditions = " OR ".join(
                    [f"context ILIKE ${i+2}" for i in range(len(keywords))]
                )
                params = [tenant_uuid] + [f"%{kw}%" for kw in keywords]
                rows = await pool.fetch(
                    f"""
                    SELECT context, solution, success_count, tech_stack,
                           0.5 AS similarity
                    FROM code_patterns
                    WHERE tenant_id = $1
                      AND pattern_type = 'success'
                      AND ({like_conditions})
                    ORDER BY success_count DESC
                    LIMIT {TOP_K}
                    """,
                    *params,
                )
                logger.info(
                    "vector_memory.ilike_fallback",
                    execution_id=execution_id,
                    hits=len(rows),
                )
        except Exception as e:
            logger.warning("vector_memory.ilike_failed", error=str(e))
            return ""

    if not rows:
        return ""

    parts = ["RELEVANT PAST SOLUTIONS (adapt, do not copy verbatim):"]
    for i, row in enumerate(rows, 1):
        sim_pct = int(row.get("similarity", 0) * 100)
        parts.append(f"\n--- Pattern {i} | used {row['success_count']}x | {sim_pct}% match ---")
        parts.append(f"Context: {row['context'][:200]}")
        parts.append(f"Stack: {row['tech_stack']}")
        parts.append(f"Solution approach:\n{row['solution'][:500]}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Design Spec Store/Retrieve (v2.6 additions)
# ---------------------------------------------------------------------------

async def store_design_spec_vector(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    design_spec: dict,
    execution_id: str,
) -> None:
    """
    Store a successful design spec with its embedding for future warm-start.
    Called after visual review passes.
    """
    try:
        # Embed the task + design system summary
        embed_input = f"Task: {task}\nStack: {tech_stack}\n"
        embed_input += f"Colors: {list(design_spec.get('design_system', {}).get('colors', {}).keys())}\n"
        embed_input += f"Components: {[c['name'] for c in design_spec.get('components', [])]}"

        embedding = await _embed(embed_input)

        if embedding is not None:
            await pool.execute(
                """
                INSERT INTO design_specs
                    (tenant_id, task_context, tech_stack, spec_json, embedding, hit_count)
                VALUES ($1, $2, $3, $4, $5::vector, 1)
                ON CONFLICT (tenant_id, task_context_hash)
                DO UPDATE SET
                    spec_json = EXCLUDED.spec_json,
                    embedding = EXCLUDED.embedding,
                    hit_count = design_specs.hit_count + 1,
                    updated_at = NOW()
                """,
                tenant_uuid,
                task[:200],
                tech_stack,
                json.dumps(design_spec),
                json.dumps(embedding),
            )
            logger.info("vector_memory.design_stored", execution_id=execution_id)
    except Exception as e:
        logger.warning("vector_memory.design_store_failed", error=str(e), execution_id=execution_id)


async def retrieve_design_spec_vector(
    pool,
    tenant_uuid: str,
    task: str,
    tech_stack: str,
    execution_id: str,
) -> Optional[dict]:
    """
    Retrieve a relevant past design spec using pgvector cosine similarity.
    Falls back to None if no match or unavailable.
    Returns parsed design spec dict or None.
    """
    query_embedding = await _embed(f"Task: {task}\nStack: {tech_stack}")

    if query_embedding is None:
        return None

    try:
        rows = await pool.fetch(
            """
            SELECT spec_json, 1 - (embedding <=> $2::vector) AS similarity
            FROM design_specs
            WHERE tenant_id = $1
              AND tech_stack = $3
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> $2::vector) >= $4
            ORDER BY similarity DESC
            LIMIT 1
            """,
            tenant_uuid,
            json.dumps(query_embedding),
            tech_stack,
            SIMILARITY_THRESHOLD,
        )

        if rows:
            spec = json.loads(rows[0]["spec_json"])
            sim_pct = int(rows[0]["similarity"] * 100)
            logger.info(
                "vector_memory.design_retrieved",
                execution_id=execution_id,
                similarity=sim_pct,
            )
            return spec

    except Exception as e:
        logger.warning("vector_memory.design_retrieve_failed", error=str(e))

    return None
