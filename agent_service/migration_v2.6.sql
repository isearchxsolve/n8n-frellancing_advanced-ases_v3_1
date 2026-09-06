-- ASES v2.6 Database Migration
-- Adds tables for design spec vector memory and interaction test history

-- Design specs table (for warm-starting design_agent)
CREATE TABLE IF NOT EXISTS design_specs (
    id SERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_context TEXT NOT NULL,
    task_context_hash TEXT GENERATED ALWAYS AS (encode(digest(task_context, 'sha256'), 'hex')) STORED,
    tech_stack TEXT NOT NULL,
    spec_json JSONB NOT NULL,
    embedding VECTOR(1536),
    hit_count INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, task_context_hash)
);

-- Index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_design_specs_embedding 
ON design_specs USING ivfflat (embedding vector_cosine_ops)
WHERE embedding IS NOT NULL;

-- Index for tenant + tech stack lookups
CREATE INDEX IF NOT EXISTS idx_design_specs_tenant_tech 
ON design_specs(tenant_id, tech_stack);

-- Interaction test results (for debugging and pattern analysis)
CREATE TABLE IF NOT EXISTS interaction_test_results (
    id SERIAL PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    test_name TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    error TEXT,
    stage TEXT,
    screenshot_b64 TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_interaction_results_execution 
ON interaction_test_results(execution_id);

-- Update function for updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for design_specs updated_at
DROP TRIGGER IF EXISTS update_design_specs_updated_at ON design_specs;
CREATE TRIGGER update_design_specs_updated_at
    BEFORE UPDATE ON design_specs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
