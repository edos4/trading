# Pattern Editor PostgreSQL storage (P02)

P02 implements the PostgreSQL repository and schema. File inventory/parity/bootstrap publication is P03; application-wide discovery and pinned-version cutover is P04. This is not a completed Patterns feature deployment.

## Configuration and migration

Install the project's existing `psycopg[binary]` dependency and provision a PostgreSQL database. Verified here against PostgreSQL 17.11. `PATTERN_EDITOR_DATABASE_URL` selects an optional separate editor database; when empty, the existing `DATABASE_URL` / `db_*` configuration is used. `PATTERN_EDITOR_TEST_DATABASE_URL` is exclusively for tests and never supplies runtime configuration. See `.env.example`; keep credentials out of source and command output.

Migration and runtime use the `pattern_editor` schema. The connection role needs schema/table/function DDL privileges for migration. The runtime role needs schema usage and table CRUD/sequence privileges as appropriate; production role provisioning and worker lifecycle belong to rollout work. Immutable-record triggers reject updates, deletion, and truncation even for ordinary SQL writers. As with any database, an owner/superuser capable of altering schema can bypass these protections.

From the repository with its normal application Settings configured:

```bash
.venv/bin/python scripts/migrate_pattern_editor.py
```

Expected first-run output: `Applied editor migrations: 1`. Repeat output: `Applied editor migrations: already up to date`.

The runner:

- Uses numbered/checksummed SQL files in `migrations/pattern_editor/`, an advisory transaction lock, and atomic application of pending migrations.
- Rejects changed/unknown applied migration history; add a new migration instead of editing one already deployed.
- Creates only the editor schema/tables, never the database or market-history tables.
- Is separate from repository construction, reads, and application startup. A missing/mismatched schema produces `MigrationRequired`; the repository does not repair it automatically.
- Bounds connection, lock, statement, and idle-transaction waits (5 seconds, 5 seconds, 20 seconds, and 30 seconds respectively). Transactions must stay short; do not hold them across AI calls or backtests.
- Suppresses raw connection/server error details in user-facing exceptions. PostgreSQL outage/privilege/configuration errors never create a file database or switch to a file-backed artifact store.

## Storage layout and invariants

`content_blobs` uses `BYTEA` with database SHA-256 and byte-length checks; reads verify both again. References contain only `sha256`, `size_bytes`, and `media_type`, never filesystem paths. Identical bytes deduplicate by hash; MIME type is contextual reference metadata. Source bytes retain original encoding, line endings, null bytes, and non-UTF-8 bytes.

The schema includes:

- `patterns`, immutable `versions`/`version_files`, and separate `version_lifecycle`.
- Immutable `import_batches` manifests with mutable `import_state` and linked reports.
- `sessions`, immutable `revisions`/`reports`, `presets`, `workers`, durable `jobs` and lease columns.
- Immutable `backtest_runs`, `run_versions`, `results`, audit `events`, and activation-operation records.

Foreign keys protect content/history references and enforce same-pattern parents/defaults. Defaults require a lifecycle row and cannot reference archived versions. Enabled published patterns require a default. Unpublished staged patterns never appear in `active_set()`.

`EditStore.insert_version()` allocates monotonic numbers under a pattern row lock; committed immutable versions cannot be deleted, so archived numbers cannot be reused. A rolled-back insertion does not consume a committed version number. `update_pattern()` and `save_session()` perform generation checks. Default replacement and archival can share a transaction; deferred constraints allow the final consistent state. Duplicate job/activation idempotency keys are rejected by uniqueness constraints; same-request replay semantics and lease claiming/recovery are P05/P08 work.

Each repository operation opens/closes its own psycopg connection. Pass the provided transaction connection for multi-record atomicity. Do not pass an open connection to spawned workers. The constructor performs no PostgreSQL migration or editor-directory creation.

Domain eligibility (successful validation/backtest before default selection), final import verification/publication, and full job/result adapters remain assigned to P03–P05. The storage CAS is not a public endpoint that bypasses those future checks.

## Compatibility boundary

The former `PatternVersions.baseline()` helper now stages its source snapshot and blobs in PostgreSQL. It does not publish a default or write a source manifest/artifact directory. It remains a compatibility API for existing validation/loader tests, not the complete P03 import command: its static dependency collection, baseline payload format, and runtime fingerprint must still be replaced/extended by the verified ten-pattern bootstrap.

Legacy source-mirror recovery is fail-closed: staged/registered journals cause an explicit reconciliation error and never rewrite detector files. Worker heartbeat SQL uses the PostgreSQL repository. Existing SQLite files/artifacts are untouched and are not read by the new repository.

The loader's legacy file-existence guards, module discovery/fallback paths, and scanner's automatic version refresh are **not yet cut over**; P04 still owns these consumer changes. Do not treat migration alone as runtime readiness or expect old SQLite registry state to transfer. In particular, a consumer that reaches the replacement repository requires a configured, migrated PostgreSQL database. No application launch smoke test or source-catalog publication is claimed in P02.

## Tests

Set `PATTERN_EDITOR_TEST_DATABASE_URL` explicitly to a disposable PostgreSQL database. Tests never derive this from application settings or `.env`. A fixture creates a random `pattern_editor_test_<uuid>` schema, applies migrations, and drops only that generated schema on teardown. Use a dedicated test role/database; the fixture never needs production credentials. With no test DSN these integration tests report skips, which do not satisfy the P02 gate.

```bash
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ .venv/bin/python -m pytest -q core/test_pattern_edit_store.py core/test_pattern_versions.py core/test_pattern_loader.py core/test_pattern_edit_validation.py core/test_pattern_editor_contracts.py
```

For a private local test cluster, substitute an installed PostgreSQL server version and retain the generated directory for shutdown:

```bash
pg_test_root=$(mktemp -d /tmp/pattern-editor-pg.XXXXXX)
mkdir "$pg_test_root/socket"
/usr/lib/postgresql/17/bin/initdb -D "$pg_test_root/data" -A trust --no-locale -E UTF8
/usr/lib/postgresql/17/bin/pg_ctl -D "$pg_test_root/data" -l "$pg_test_root/server.log" -o "-k $pg_test_root/socket -p 55482 -h ''" -w start
psql -h "$pg_test_root/socket" -p 55482 -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE pattern_editor_test'
export PATTERN_EDITOR_TEST_DATABASE_URL="postgresql:///pattern_editor_test?host=$pg_test_root/socket&port=55482"
# Run the test command above, then stop the private cluster:
/usr/lib/postgresql/17/bin/pg_ctl -D "$pg_test_root/data" -m fast -w stop
```

The temporary root is private to its owner, authentication is for this isolated cluster only, and TCP listening is disabled. In a tool sandbox, Unix socket startup/access may need an execution permission override; no production database is needed.

## Backup and restore commands

These commands are documented for operators; a production backup/restore rehearsal is P11. First set `PATTERN_EDITOR_DATABASE_URL` to the intended PostgreSQL database, explicitly even if runtime normally reuses `DATABASE_URL`. Stop editor writes/jobs for a consistent deployment boundary. Use a PostgreSQL client compatible with the server.

```bash
# Fail if the explicit deployment DSN or backup destination is absent.
: "${PATTERN_EDITOR_DATABASE_URL:?Set the intended editor PostgreSQL DSN}"
: "${EDITOR_BACKUP_PATH:?Choose a new protected backup file path}"
pg_dump --dbname="$PATTERN_EDITOR_DATABASE_URL" --schema=pattern_editor --format=custom --file="$EDITOR_BACKUP_PATH"
pg_restore --list "$EDITOR_BACKUP_PATH"
```

Restore first into an empty, separately provisioned recovery database; set `EDITOR_RESTORE_DATABASE_URL` to that destination:

```bash
: "${EDITOR_RESTORE_DATABASE_URL:?Set the empty recovery database DSN}"
pg_restore --dbname="$EDITOR_RESTORE_DATABASE_URL" --exit-on-error --single-transaction --no-owner --no-privileges "$EDITOR_BACKUP_PATH"
```

Restore application role grants separately if needed, use application code/migration files matching the backup's schema checksums, verify blob hashes/default references and reports there, then plan the target cutover. Do not automatically downgrade migrations or fall back to SQLite. Live replacement/rollback requires the concrete P11 deployment procedure; the commands above intentionally do not drop a running target schema.

## P02 verification record

- Isolated PostgreSQL **17.11**, Unix socket under `/tmp/pattern-editor-p02-pg/socket`, port 55482, database `pattern_editor_test`; no TCP listener. No application DSN/data was used.
- Final focused suite: **47 passed in 3.17 seconds**, no skips. [Recorded output](verification/pattern-editor-p02-tests.txt).
- Tests cover concurrent schema initialization, checksum mismatch, missing schema, migration rollback, spawned concurrent version allocation, source/blob immutability and digest/size enforcement, transactional rollback, same-pattern/default/archive constraints, CAS conflicts, durable graph relationships, artifact-reference rejection, session/revision/report persistence, worker upsert, redacted failures, and real connection failure without file creation.
- Ported tests use real PostgreSQL; fake validator workers remain deliberate unit doubles and are not proof of a configured production candidate sandbox.
- CLI smoke test on the disposable database: first run applied migration 1; second run was a no-op. No baseline catalog was published.
- The initial sandbox blocked PostgreSQL Unix sockets; execution overrides allowed only the private test server/test commands. Full sandbox execution for generated pattern code remains a later prerequisite.
- Schema migration: `0001_registry.sql`. Test schemas were removed by fixture cleanup; the private PostgreSQL cluster was stopped after verification. Production rollout: **not performed**.

Resume at **P03-01** in [the implementation tracker](pattern-editor-implementation-plan.md).
