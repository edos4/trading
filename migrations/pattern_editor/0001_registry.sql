-- Applied only by the explicit migration runner with a schema-qualified search_path.
CREATE TABLE content_blobs (
    sha256 TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    data BYTEA NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0 AND size_bytes = octet_length(data)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (sha256 = encode(sha256(data), 'hex'))
);

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY,
    snapshot_sha256 TEXT NOT NULL UNIQUE CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    manifest JSONB NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE import_state (
    batch TEXT PRIMARY KEY REFERENCES import_batches(id),
    state TEXT NOT NULL DEFAULT 'staged' CHECK (state IN ('staged','verified','published','failed')),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    report_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE patterns (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    active TEXT,
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    next_version BIGINT NOT NULL DEFAULT 1 CHECK (next_version > 0),
    CHECK (NOT (published AND enabled) OR active IS NOT NULL)
);
CREATE TABLE versions (
    id TEXT PRIMARY KEY,
    pattern TEXT NOT NULL REFERENCES patterns(id),
    number BIGINT NOT NULL CHECK (number > 0),
    parent TEXT,
    import_batch TEXT REFERENCES import_batches(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    UNIQUE(pattern, number), UNIQUE(pattern, id),
    FOREIGN KEY(pattern, parent) REFERENCES versions(pattern, id),
    CHECK (parent IS NULL OR parent <> id),
    CHECK (payload->>'version_id' IS NOT NULL AND payload->>'version_id' = id),
    CHECK (payload->>'pattern_id' IS NOT NULL AND payload->>'pattern_id' = pattern),
    CHECK (payload->>'version_number' IS NOT NULL AND (payload->>'version_number')::BIGINT = number),
    CHECK ((payload->>'parent_version_id') IS NOT DISTINCT FROM parent)
);
ALTER TABLE patterns ADD CONSTRAINT default_same_pattern
    FOREIGN KEY (id, active) REFERENCES versions(pattern, id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE version_files (
    version TEXT NOT NULL REFERENCES versions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    media_type TEXT NOT NULL,
    PRIMARY KEY(version, path)
);
CREATE TABLE version_lifecycle (
    version TEXT PRIMARY KEY REFERENCES versions(id),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    validation TEXT NOT NULL DEFAULT 'pending' CHECK (validation IN ('pending','passed','failed','blocked')),
    validation_report_id TEXT,
    baseline_report_id TEXT,
    successful_run_id TEXT,
    archived_at TIMESTAMPTZ
);
-- Even an unpublished default must have a lifecycle row.
ALTER TABLE patterns ADD CONSTRAINT default_has_lifecycle
    FOREIGN KEY(active) REFERENCES version_lifecycle(version) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id),
    generation BIGINT NOT NULL CHECK(generation >= 0), payload JSONB NOT NULL
);
CREATE TABLE revisions (
    id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id), payload JSONB NOT NULL
);
CREATE TABLE reports (
    id TEXT PRIMARY KEY, revision TEXT REFERENCES revisions(id), version TEXT REFERENCES versions(id),
    batch TEXT REFERENCES import_batches(id), payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE import_state ADD FOREIGN KEY(report_id) REFERENCES reports(id);
ALTER TABLE version_lifecycle ADD FOREIGN KEY(validation_report_id) REFERENCES reports(id);
ALTER TABLE version_lifecycle ADD FOREIGN KEY(baseline_report_id) REFERENCES reports(id);
CREATE TABLE presets (
    id TEXT PRIMARY KEY, generation BIGINT NOT NULL DEFAULT 0 CHECK(generation >= 0),
    payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE workers (
    id TEXT PRIMARY KEY, heartbeat TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('edit','backtest')),
    session TEXT REFERENCES sessions(id), pattern TEXT REFERENCES patterns(id),
    base_version TEXT REFERENCES versions(id), preset TEXT REFERENCES presets(id),
    generated_version TEXT REFERENCES versions(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN
        ('queued','generating','validating','backtesting','completed','failed','cancelled','blocked','interrupted')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt > 0),
    retry_of TEXT REFERENCES jobs(id),
    owner TEXT REFERENCES workers(id), lease_token TEXT, lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK ((owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR
           (owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX claimable_jobs ON jobs(state,created_at);
CREATE INDEX expiring_jobs ON jobs(lease_expires_at) WHERE owner IS NOT NULL;
CREATE TABLE backtest_runs (
    id TEXT PRIMARY KEY, job TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    inputs_sha256 TEXT NOT NULL CHECK(inputs_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE run_versions (
    run TEXT NOT NULL REFERENCES backtest_runs(id), pattern TEXT NOT NULL,
    version TEXT NOT NULL, PRIMARY KEY(run,pattern),
    FOREIGN KEY(pattern,version) REFERENCES versions(pattern,id)
);
CREATE TABLE results (
    id TEXT PRIMARY KEY, run TEXT NOT NULL UNIQUE REFERENCES backtest_runs(id),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE version_lifecycle ADD FOREIGN KEY(successful_run_id) REFERENCES backtest_runs(id);
CREATE TABLE activations (
    id TEXT PRIMARY KEY, pattern TEXT NOT NULL REFERENCES patterns(id),
    idempotency_key TEXT NOT NULL UNIQUE, payload JSONB NOT NULL
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, pattern TEXT REFERENCES patterns(id),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION reject_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable editor record' USING ERRCODE = '23514';
END $$;
DO $$ DECLARE tab TEXT; BEGIN
    FOREACH tab IN ARRAY ARRAY['content_blobs','versions','version_files','import_batches',
                              'revisions','reports','backtest_runs','run_versions','results','events'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_row BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()', tab);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_change()', tab);
    END LOOP;
END $$;

-- Serialize lifecycle updates with default changes, including direct SQL callers.
CREATE FUNCTION lock_lifecycle_pattern() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM patterns WHERE id = (SELECT pattern FROM versions WHERE id=NEW.version) FOR UPDATE;
    RETURN NEW;
END $$;
CREATE TRIGGER lifecycle_pattern_lock BEFORE UPDATE ON version_lifecycle
    FOR EACH ROW EXECUTE FUNCTION lock_lifecycle_pattern();
CREATE FUNCTION check_default_available() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pattern_id TEXT; BEGIN
    IF TG_TABLE_NAME='patterns' THEN pattern_id := NEW.id;
    ELSE SELECT pattern INTO pattern_id FROM versions WHERE id=NEW.version;
    END IF;
    IF EXISTS (SELECT 1 FROM patterns p JOIN version_lifecycle l ON l.version=p.active
               WHERE p.id=pattern_id AND l.archived_at IS NOT NULL) THEN
        RAISE EXCEPTION 'default version is archived' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER valid_default AFTER INSERT OR UPDATE ON patterns
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_default_available();
CREATE CONSTRAINT TRIGGER default_not_archived AFTER INSERT OR UPDATE ON version_lifecycle
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_default_available();
