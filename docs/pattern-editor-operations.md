# Pattern Editor operations (P11)

Operator guide for the PostgreSQL pattern editor, both frontends, and the AI
edit pipeline. PostgreSQL is the only durable editor store; local files are
import inputs, exports, or disposable sandbox materializations. There is no
SQLite or file-store fallback in any path.

## Components

| Piece | Location |
| --- | --- |
| Repository / schema | `core/pattern_edit_store.py`, `core/pattern_editor_db.py`, `migrations/pattern_editor/0001_registry.sql` |
| Migration CLI | `scripts/migrate_pattern_editor.py` |
| Exact bootstrap import | `scripts/import_pattern_baselines.py` (`core/pattern_bootstrap.py`) |
| Version lifecycle | `core/pattern_versions.py` |
| Loader / pinned consumers | `core/pattern_loader.py`, `core/scanner.py`, `core/backtester.py` |
| Backtest service + stream replay | `core/backtest_service.py`, `core/stream_backtest.py`, `core/backtest_jobs.py` |
| Edit coordinator + provider | `core/pattern_edit_service.py`, `ai/providers/deepseek.py` |
| Shared UI facade | `core/pattern_editor_api.py` |
| Web tab | `web/templates/patterns.html`, `web/static/patterns.js`, `web/app.py`, `web/patterns.py` |
| Desktop dialog | `ui/patterns_dialog.py`, `ui/app.py` |

## Configuration (server-side only)

```
# Editor database. Empty reuses DATABASE_URL / db_* market-history settings.
PATTERN_EDITOR_DATABASE_URL=postgresql://user:pass@host:5432/dbname
# Candidate sandbox (both required for generated code to execute).
PATTERN_EDIT_CGROUP_ROOT=/sys/fs/cgroup/<delegated-subtree>
PATTERN_EDIT_WORKER_PYTHON=/path/to/.venv/bin/python
# Frozen daily-bar dataset for offline + stream backtests.
BACKTEST_DATASET_DIR=data/barcache
# DeepSeek (never logged or returned to a client).
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MAX_RETRIES=2
DEEPSEEK_MAX_CONCURRENCY=2
# Web auth (mandatory for --web).
WEB_UI_USERNAME=admin
WEB_UI_PASSWORD=
WEB_UI_SECRET_KEY=
WEB_UI_PORT=8080
```

`PATTERN_EDITOR_TEST_DATABASE_URL` is for tests only and never supplies runtime
configuration. Keep credentials out of examples, logs, and evidence.

## First-time setup

```bash
# 1. Provision a PostgreSQL database and role (DDL privileges for migration).
# 2. Apply the editor schema (idempotent; reruns are a no-op).
.venv/bin/python scripts/migrate_pattern_editor.py
#    first run:  "Applied editor migrations: 1"
#    rerun:      "Applied editor migrations: already up to date"

# 3. Import the exact working-tree detector bytes and publish the catalog.
.venv/bin/python scripts/import_pattern_baselines.py
#    prints {batch_id, report_id, snapshot_sha256, status}; status must be "passed"
```

The import freezes the actual working-tree bytes (including uncommitted
changes), captures transitive helpers, stages ten version-1 records, verifies
byte/hash equality and behavioral parity, and publishes atomically. A failed or
incomplete import leaves no partially published catalog and can be retried. An
empty catalog makes consumer discovery fail with an explicit bootstrap error —
never a silent file fallback.

## Launching

```bash
.venv/bin/python main.py --web     # Patterns tab at /patterns (authenticated)
.venv/bin/python main.py --ui      # Patterns toolbar button beside Kronos
```

Both launch modes must be able to run and cancel a historical stream backtest
and inspect persistent results; the web page renders even when the editor
catalog database is down (the panel reports the actionable error).

## First preset

An automatic edit requires a saved historical-stream preset. Open **Patterns**,
leave the preset selector blank, name a preset, and fill the replay settings
(market, symbols or universe, start/end date or session count, warmup, capital,
notional, costs, slippage, end policy). Press **Submit** once; the settings are
persisted as a named preset and reused on later submissions. The server rejects
an offline or incomplete preset instead of choosing defaults for you.

## Default and delete semantics

- A new AI version never becomes the default automatically. **Set default**
  requires successful validation and a successful backtest; a stale
  (generation-conflicting) request is rejected with a conflict and the page
  reloads.
- **Delete version** archives it. Deleting the current default requires a
  replacement chosen in the same transaction. At least one usable default is
  always protected. Archived versions stay readable for historical runs and
  pinned sessions but cannot be selected for new runs or reused as defaults.
- Changing a default affects new runs only; an in-flight job keeps its pinned
  version set (including spawned workers).

## Job retry

- **Backtest again** re-runs only the automatic backtests for a recorded edit
  job. It never calls the provider and never creates another version, so a
  transient data/backtest failure is recoverable without another model call.
- A leaked lease (process crash) marks the job `interrupted`; it is not re-run
  silently. Retry creates a traceable new attempt.
- A blocked job (unavailable provider or sandbox) reports `blocked`, keeps any
  version already generated, and is retryable.

## Error codes

| Code | Meaning | Status (web) |
| --- | --- | --- |
| `invalid-request` | Malformed/contradictory request | 400 |
| `not-found` | Unknown job/run | 404 |
| `conflict` | Stale generation, duplicate/foreign idempotency key | 409 |
| `database-unavailable` / migration required | PostgreSQL down or schema not migrated | 503 |
| `bootstrap-required` | Empty catalog | 400 with explicit message |
| `sandbox-unavailable` | Candidate sandbox not configured/unavailable | job `blocked` |
| `provider-failed` | DeepSeek missing/malformed/timeout | `blocked` (unavailable) or `failed` |
| `validation-failed` | Candidate failed validation | job `failed`, version preserved |
| `execution-failed` | Data/engine/backtest failure | job `failed`, version preserved |
| `runtime-incompatible` | Version runtime fingerprint mismatch | explicit error, no substitution |

## Backup and restore

Stop editor writes/jobs for a consistent boundary. Use a PostgreSQL client
compatible with the server.

```bash
: "${PATTERN_EDITOR_DATABASE_URL:?Set the intended editor PostgreSQL DSN}"
: "${EDITOR_BACKUP_PATH:?Choose a new protected backup file path}"
pg_dump --dbname="$PATTERN_EDITOR_DATABASE_URL" --schema=pattern_editor \
        --format=custom --file="$EDITOR_BACKUP_PATH"
pg_restore --list "$EDITOR_BACKUP_PATH"

: "${EDITOR_RESTORE_DATABASE_URL:?Set the empty recovery database DSN}"
pg_restore --dbname="$EDITOR_RESTORE_DATABASE_URL" --exit-on-error --single-transaction \
           --no-owner --no-privileges "$EDITOR_BACKUP_PATH"
```

Restore first into an empty, separately provisioned recovery database; verify
blob hashes, default references and reports there before any cutover. Rollback
restores a verified backup or the prior application deployment — it is not an
automatic runtime storage fallback.

## Runtime compatibility failures

Trusted engine/interface code is application code with a recorded compatibility
manifest. If a version's runtime fingerprint or a trusted file hash no longer
matches, loading raises an explicit error instead of silently loading a
different file or version. Restore the matching runtime or re-import; do not
edit the immutable version.

## Tests

```bash
PATTERN_EDITOR_TEST_DATABASE_URL=postgresql:///pattern_editor_test?host=/tmp/sock&port=55484 \
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ DISPLAY=:1 \
.venv/bin/python -m pytest -q core/test_patterns_web.py core/test_patterns_desktop.py
```

Tests create a random `pattern_editor_test_<uuid>` schema and drop only that
schema. With no test DSN the integration tests report skips, which do not
satisfy any gate.
