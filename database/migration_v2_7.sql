-- =============================================================================
-- ASES v2.7 Migration — Vector Memory + Interface Signature Cache
-- =============================================================================
-- Run against your existing ases_production database:
--   psql $DATABASE_URL -f migration_v2_7.sql
--
-- Safe to re-run (all statements use IF NOT EXISTS / ON CONFLICT).
-- Estimated time: < 5 seconds on a fresh DB, < 30s if code_patterns is large.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Enable pgvector extension (requires pgvector installed in Postgres image)
--    Docker: use pgvector/pgvector:pg16 instead of plain postgres:16
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 2. Add embedding column to code_patterns (Gap Fix: vector memory)
-- ---------------------------------------------------------------------------
ALTER TABLE code_patterns
    ADD COLUMN IF NOT EXISTS embedding vector(1536),
    ADD COLUMN IF NOT EXISTS tech_stack VARCHAR(100),
    ADD COLUMN IF NOT EXISTS context_hash VARCHAR(64)
        GENERATED ALWAYS AS (md5(context)) STORED;

-- Unique constraint for upsert in store_memory_pattern_vector
ALTER TABLE code_patterns
    DROP CONSTRAINT IF EXISTS code_patterns_tenant_context_uq;
ALTER TABLE code_patterns
    ADD CONSTRAINT code_patterns_tenant_context_uq
        UNIQUE (tenant_id, context_hash);

-- Index for cosine similarity search (IVFFlat — good for < 1M rows)
-- lists=100 is appropriate for up to ~500k rows per tenant
CREATE INDEX IF NOT EXISTS idx_code_patterns_embedding
    ON code_patterns USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Fallback index for ILIKE keyword search (already exists in most deployments)
CREATE INDEX IF NOT EXISTS idx_code_patterns_context
    ON code_patterns USING gin (to_tsvector('english', context));

-- ---------------------------------------------------------------------------
-- 3. Interface signature cache (Gap Fix: cross-job differ cold start)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interface_signatures (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tech_stack      VARCHAR(100) NOT NULL,
    file_pattern    VARCHAR(255) NOT NULL,   -- e.g. "routes/auth.js"
    exports         JSONB NOT NULL DEFAULT '[]',
    imports_from    JSONB NOT NULL DEFAULT '{}',
    hit_count       INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id, tech_stack, file_pattern)
);

CREATE INDEX IF NOT EXISTS idx_iface_sigs_tenant_stack
    ON interface_signatures(tenant_id, tech_stack, hit_count DESC);

-- Auto-update updated_at
CREATE TRIGGER IF NOT EXISTS trigger_iface_sigs_updated_at
    BEFORE UPDATE ON interface_signatures
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Verify
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ASSERT (SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'code_patterns' AND column_name = 'embedding') = 1,
        'embedding column missing from code_patterns';

    ASSERT (SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'interface_signatures') = 1,
        'interface_signatures table missing';

    RAISE NOTICE 'ASES v2.7 migration complete.';
END $$;
