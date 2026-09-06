-- ASES v3.0 Migration
-- Adds two tables required by the learned failure classifier.
-- Safe to run on a live database — both statements use IF NOT EXISTS.
-- No existing tables are modified.

-- Stores the serialised per-tenant logistic regression model.
-- Upserted by failure_classifier.train_classifier_from_journal() after
-- each successful job once >= 20 labeled samples exist.
CREATE TABLE IF NOT EXISTS tenant_classifiers (
    tenant_id   TEXT        NOT NULL,
    classifier  JSONB       NOT NULL,   -- {weights, intercept, vocab, trained_at, n_samples, train_accuracy}
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id)
);

-- Ground-truth labeled failure descriptions used to train the classifier.
-- Populated by failure_classifier.store_training_sample():
--   label=1  → failure was design-level (design regen was triggered)
--   label=0  → failure was code-level  (coder fixed without regen)
-- source values: 'visual_regen' | 'coder_fixed' | 'interaction_regen'
CREATE TABLE IF NOT EXISTS classifier_training_data (
    id          BIGSERIAL   PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    description TEXT        NOT NULL,
    label       SMALLINT    NOT NULL CHECK (label IN (0, 1)),
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_classifier_training_tenant
    ON classifier_training_data (tenant_id, created_at DESC);
