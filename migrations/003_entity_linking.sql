CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS entity_aliases (
    id BIGSERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    alias_type TEXT NOT NULL DEFAULT 'synonym',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    document_id TEXT NOT NULL DEFAULT '',
    surface_form TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    entity_type_hint TEXT,
    resolved_entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    source_url TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}',
    candidates JSONB NOT NULL DEFAULT '[]',
    linker_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_assertions (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    source_mention_id BIGINT REFERENCES entity_mentions(id) ON DELETE SET NULL,
    target_mention_id BIGINT REFERENCES entity_mentions(id) ON DELETE SET NULL,
    subject_entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    object_entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    value JSONB,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    source_url TEXT NOT NULL DEFAULT '',
    evidence_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS linking_review_queue (
    id BIGSERIAL PRIMARY KEY,
    mention_id BIGINT NOT NULL REFERENCES entity_mentions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    candidates JSONB NOT NULL DEFAULT '[]',
    resolved_entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS entity_embeddings (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized ON entity_aliases (normalized_alias);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_surface ON entity_mentions (normalized_surface);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_resolved ON entity_mentions (resolved_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_status ON entity_mentions (status);
CREATE INDEX IF NOT EXISTS idx_entity_assertions_subject ON entity_assertions (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_assertions_status ON entity_assertions (status);
CREATE INDEX IF NOT EXISTS idx_linking_review_pending
    ON linking_review_queue (status) WHERE status = 'pending';
