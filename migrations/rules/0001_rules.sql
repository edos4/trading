-- Visual indicator rules: named condition trees used as strategies or filters.
--
-- The migration runner owns the schema, schema_migrations and search_path;
-- this file contains DDL only. One row per user-created indicator. The whole
-- condition tree lives in payload; generation serialises concurrent edits.

CREATE TABLE indicator_rules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    generation  BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    payload     JSONB NOT NULL CHECK (
                    jsonb_typeof(payload) = 'object'
                    AND payload->>'schema_version' = '1'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX indicator_rules_name ON indicator_rules (lower(name));
CREATE INDEX indicator_rules_updated ON indicator_rules (updated_at DESC);
