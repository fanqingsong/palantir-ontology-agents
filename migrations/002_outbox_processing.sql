-- Outbox claim fields for Prefect / multi-consumer projection.
-- Guard DDL so re-applying this file does not take AccessExclusiveLock on outbox.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'outbox' AND column_name = 'locked_by'
    ) THEN
        ALTER TABLE outbox ADD COLUMN locked_by TEXT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'outbox' AND column_name = 'locked_until'
    ) THEN
        ALTER TABLE outbox ADD COLUMN locked_until TIMESTAMPTZ;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_outbox_pending_id'
    ) THEN
        CREATE INDEX idx_outbox_pending_id ON outbox (id) WHERE status = 'pending';
    END IF;
END $$;

-- Debezium logical replication (publication created idempotently)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'dbz_outbox') THEN
        CREATE PUBLICATION dbz_outbox FOR TABLE outbox;
    END IF;
END $$;

-- Replication user privilege for Debezium (ontology is compose default user)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology') THEN
        ALTER USER ontology REPLICATION;
    END IF;
END $$;
