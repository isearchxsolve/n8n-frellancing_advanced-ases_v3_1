-- ============================================================
-- ASES Gap Fixes — DB Migration
-- Run after existing init.sql
-- ============================================================

-- Fix 1: Store iteration journals per execution (optional persistence)
-- The IterationJournal is in-memory per run by default.
-- Add this table only if you want journals queryable after the fact.
CREATE TABLE IF NOT EXISTS iteration_journals (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    execution_id    UUID NOT NULL,
    iteration       INTEGER NOT NULL,
    file_paths      TEXT[],
    architectural_decisions TEXT[],
    test_passed     BOOLEAN NOT NULL DEFAULT FALSE,
    static_passed   BOOLEAN,
    reviewer_approved BOOLEAN,
    errors          TEXT[],
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_execution
    ON iteration_journals(execution_id, iteration);

-- Fix 3: Store clarification events for analytics and debugging
CREATE TABLE IF NOT EXISTS clarification_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    execution_id    UUID NOT NULL,
    task_snippet    TEXT,
    clarity_score   NUMERIC(4,2),
    action          VARCHAR(30),    -- PROCEED | CLARIFICATION_NEEDED
    questions       JSONB,
    assumptions     JSONB,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clarification_tenant
    ON clarification_events(tenant_id, recorded_at DESC);

-- Fix 4: Store visual review results (screenshot hash + outcome)
CREATE TABLE IF NOT EXISTS visual_reviews (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    execution_id    UUID NOT NULL,
    iteration       INTEGER NOT NULL,
    approved        BOOLEAN NOT NULL,
    issues          JSONB,
    screenshot_hash VARCHAR(64),    -- SHA256 of PNG, not storing the PNG itself
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_visual_review_execution
    ON visual_reviews(execution_id);

-- TenantConfig additions (document as comments — enforced in models.py)
-- require_clarity   BOOLEAN DEFAULT FALSE  -- block underspecified jobs
-- clarity_threshold NUMERIC DEFAULT 5.0   -- 0-10 threshold to trigger questions
