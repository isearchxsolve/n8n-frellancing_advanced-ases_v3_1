-- ASES Database Schema
-- PostgreSQL 16
-- Multi-tenant, audit-friendly, production-ready

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- TENANTS
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    -- API key is stored as a SHA-256 hex digest so the plaintext never
    -- touches the DB. Compare with encode(digest($input,'sha256'),'hex').
    api_key_hash VARCHAR(64) UNIQUE,
    config JSONB NOT NULL DEFAULT '{}',
    plan VARCHAR(50) NOT NULL DEFAULT 'free',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookup by API key hash (used on every authenticated request)
CREATE INDEX idx_tenants_api_key_hash ON tenants(api_key_hash);

CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_status ON tenants(status);

-- ---------------------------------------------------------------------------
-- JOBS (Lead Pipeline)
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    link TEXT NOT NULL,
    pub_date TIMESTAMPTZ,

    -- Scoring
    score DECIMAL(3,1) NOT NULL DEFAULT 0,
    score_reason TEXT,
    red_flags JSONB DEFAULT '[]',

    -- Proposal
    proposal TEXT,

    -- Status
    status VARCHAR(50) NOT NULL DEFAULT 'new',
    -- new, scored, pending, submitted, won, lost, rejected

    -- Metadata
    source VARCHAR(50) NOT NULL DEFAULT 'upwork',
    raw_data JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(tenant_id, job_id)
);

CREATE INDEX idx_jobs_tenant ON jobs(tenant_id);
CREATE INDEX idx_jobs_status ON jobs(tenant_id, status);
CREATE INDEX idx_jobs_score ON jobs(tenant_id, score);
CREATE INDEX idx_jobs_created ON jobs(tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- CLIENTS (CRM)
-- ---------------------------------------------------------------------------
CREATE TABLE clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    company VARCHAR(255),
    project_type VARCHAR(100),
    budget DECIMAL(12,2),
    status VARCHAR(50) NOT NULL DEFAULT 'lead',
    source VARCHAR(100),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(tenant_id, client_id)
);

CREATE INDEX idx_clients_tenant ON clients(tenant_id);
CREATE INDEX idx_clients_status ON clients(tenant_id, status);

-- ---------------------------------------------------------------------------
-- CLIENT NOTES
-- ---------------------------------------------------------------------------
CREATE TABLE client_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    note TEXT NOT NULL,
    author VARCHAR(100) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notes_client ON client_notes(client_id);
CREATE INDEX idx_notes_tenant ON client_notes(tenant_id);

-- ---------------------------------------------------------------------------
-- PAYMENTS
-- ---------------------------------------------------------------------------
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    amount DECIMAL(12,2) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    invoice_id VARCHAR(100),
    method VARCHAR(50),
    paid_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_payments_client ON payments(client_id);
CREATE INDEX idx_payments_tenant ON payments(tenant_id);

-- ---------------------------------------------------------------------------
-- OUTREACH (Cold Email)
-- ---------------------------------------------------------------------------
CREATE TABLE outreach (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    company VARCHAR(255),
    contact_name VARCHAR(255),
    subject TEXT,
    body TEXT,
    sent_at TIMESTAMPTZ,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    follow_up_date DATE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outreach_tenant ON outreach(tenant_id);
CREATE INDEX idx_outreach_status ON outreach(tenant_id, status);

-- ---------------------------------------------------------------------------
-- EXECUTIONS (Audit + Billing)
-- ---------------------------------------------------------------------------
CREATE TABLE executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    execution_id UUID NOT NULL UNIQUE,
    task_type VARCHAR(50) NOT NULL,
    payload JSONB,

    -- Results
    success BOOLEAN NOT NULL DEFAULT FALSE,
    result JSONB,
    error TEXT,

    -- Metrics
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    compute_seconds DECIMAL(8,2) NOT NULL DEFAULT 0,
    cost_usd DECIMAL(8,4) NOT NULL DEFAULT 0,

    -- Timing
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_executions_tenant ON executions(tenant_id);
CREATE INDEX idx_executions_type ON executions(tenant_id, task_type);
CREATE INDEX idx_executions_success ON executions(tenant_id, success);
CREATE INDEX idx_executions_date ON executions(tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- CODE PATTERNS (Memory Layer)
-- ---------------------------------------------------------------------------
CREATE TABLE code_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    pattern_hash VARCHAR(64) NOT NULL,
    pattern_type VARCHAR(50) NOT NULL,
    context TEXT NOT NULL,
    solution TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 1,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(tenant_id, pattern_hash)
);

CREATE INDEX idx_patterns_tenant ON code_patterns(tenant_id);
CREATE INDEX idx_patterns_type ON code_patterns(tenant_id, pattern_type);

-- ---------------------------------------------------------------------------
-- TRIGGERS
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_clients_updated_at
    BEFORE UPDATE ON clients
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_code_patterns_updated_at
    BEFORE UPDATE ON code_patterns
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ---------------------------------------------------------------------------
-- VIEWS
-- ---------------------------------------------------------------------------
CREATE VIEW daily_metrics AS
SELECT 
    tenant_id,
    DATE(created_at) as date,
    COUNT(*) as total_jobs,
    SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as submitted,
    SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as won,
    AVG(score) as avg_score,
    SUM(CASE WHEN score >= 7 THEN 1 ELSE 0 END) as high_quality_leads
FROM jobs
GROUP BY tenant_id, DATE(created_at);

CREATE VIEW execution_costs AS
SELECT 
    tenant_id,
    DATE(created_at) as date,
    task_type,
    COUNT(*) as count,
    SUM(tokens_input + tokens_output) as total_tokens,
    SUM(cost_usd) as total_cost,
    AVG(compute_seconds) as avg_duration
FROM executions
GROUP BY tenant_id, DATE(created_at), task_type;

-- ---------------------------------------------------------------------------
-- SANDBOX REGISTRY (persistent — survives process restarts)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sandbox_registry (
    container_name  VARCHAR(100) PRIMARY KEY,
    execution_id    UUID NOT NULL,
    workspace       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_created ON sandbox_registry(created_at);

-- ---------------------------------------------------------------------------
-- BILLING EVENTS (active enforcement audit trail)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    execution_id    UUID NOT NULL UNIQUE,
    cost_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_billing_tenant_date
    ON billing_events(tenant_id, recorded_at DESC);

-- Daily spend view (used by BillingFence.preflight)
CREATE OR REPLACE VIEW billing_daily AS
SELECT
    t.slug AS tenant_id,
    DATE(b.recorded_at) AS date,
    SUM(b.cost_usd) AS total_usd,
    SUM(b.tokens)   AS total_tokens
FROM billing_events b
JOIN tenants t ON t.id = b.tenant_id
GROUP BY t.slug, DATE(b.recorded_at);

-- ---------------------------------------------------------------------------
-- COLD LEADS (Outreach pipeline — v2.5)
-- Replaces the 'outreach' table with a proper CRM-integrated cold leads table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cold_leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255),
    email VARCHAR(255) NOT NULL,
    company VARCHAR(255),
    notes TEXT,
    outreach_status VARCHAR(50) NOT NULL DEFAULT 'pending',
    -- pending, contacted, replied, converted, unsubscribed
    last_contacted_at TIMESTAMPTZ,
    follow_up_date DATE,
    source VARCHAR(100) DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, email)
);

CREATE INDEX IF NOT EXISTS idx_cold_leads_tenant ON cold_leads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cold_leads_status ON cold_leads(tenant_id, outreach_status);
CREATE INDEX IF NOT EXISTS idx_cold_leads_followup ON cold_leads(tenant_id, follow_up_date);

CREATE TRIGGER trigger_cold_leads_updated_at
    BEFORE UPDATE ON cold_leads
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ---------------------------------------------------------------------------
-- PROMPT CACHE METRICS (v2.5 Redis cache observability)
-- Optional: tracks cache hit rate over time for cost reporting
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_cache_stats (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    hits INTEGER NOT NULL DEFAULT 0,
    misses INTEGER NOT NULL DEFAULT 0,
    tokens_saved INTEGER NOT NULL DEFAULT 0,
    cost_saved_usd NUMERIC(8,4) NOT NULL DEFAULT 0,
    UNIQUE(tenant_id, date)
);

CREATE INDEX IF NOT EXISTS idx_cache_stats_tenant_date
    ON prompt_cache_stats(tenant_id, date DESC);
