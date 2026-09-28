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
# Frozen daily-bar dataset for offline + stream backtests. Symbols missing from
# it are fetched from the history provider and cached here.
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

## Run every edit stage locally

Set `PATTERN_API_URL=` in `.env` and point `PATTERN_EDITOR_DATABASE_URL` at
local PostgreSQL, with `PATTERN_EDITOR_SCHEMA` selecting its editor schema.
An empty API URL disables the registry proxy: generation, validation, and both
automatic backtests run in the local web process. Generation still calls the
configured AI provider; historical bars may still come from the history API.

Start the database with `scripts/pg18.sh start` when it is stopped, prepare the
candidate sandbox with `scripts/sandbox.sh` after each reboot, and restart
`python main.py --web` after changing `.env`. Existing remote job links require
those database records to be copied locally first; changing the URL alone does
not move jobs or versions. Preserve existing databases when importing a copy.

## Historical data for backtests

Each run freezes its daily bars into PostgreSQL, so the run stays reproducible
even if the source data changes. Bars come from:

1. the local dataset (`BACKTEST_DATASET_DIR`, default `data/barcache`) when the
   symbol is already cached there, otherwise
2. the history API (`data/history_client`, `GET /api/history` at
   `STOCKS_HISTORY_URL`), fetched on demand and cached into that dataset.

A cache miss therefore does not block a run while the history API is reachable,
and the cache grows as you use symbols. `scripts/build_barcache.py`
still pre-builds a whole universe when you want an offline snapshot. If neither
the dataset nor the API yields bars, the request fails with
`No frozen daily history available for the selected symbols`. Bars are
de-duplicated to one per session, because the API can return both a
midnight-stamped row and the real session bar for the same day.

To stay strictly offline, pre-build the dataset (`scripts/build_barcache.py`):
a cached symbol never calls the API.

## Deployment

`./deploy_to_contabo.sh` rsyncs the code, installs dependencies, then runs
`scripts/setup_editor_db.py` **before** restarting the service. The database step
is idempotent and:

- creates the target database when it is missing (via `postgres`/`template1`);
- applies pending editor migrations;
- imports the file-derived baselines only when the catalog is still empty;
- warns (without failing) when detector files differ from the imported snapshot,
  because a changed file is a deliberate new-version operation.

A failure to reach PostgreSQL aborts the deploy before the service restarts, so
the app is never rolled out against a database that is not ready. The remote
`.env` must set `PATTERN_EDITOR_DATABASE_URL` or `DATABASE_URL`
(`PATTERN_EDITOR_DATABASE_URL` wins). Deploy without touching the database with:

```bash
SKIP_DB_SETUP=1 ./deploy_to_contabo.sh
```

To run the same step by hand from anywhere:

```bash
.venv/bin/python scripts/setup_editor_db.py
```

## First-time setup

A dedicated, user-owned PostgreSQL 18 instance serves the editor (no root
needed, and separate from the shared system clusters):

```bash
scripts/pg18.sh init      # first time only: initdb + create the stocks_history database
scripts/pg18.sh start     # start (idempotent); also: stop | restart | status | psql
```

It listens on `127.0.0.1:5433` with a private Unix socket under
`$HOME/.local/share/trading-pg18/socket`. Point the app at it in `.env`:

```
PATTERN_EDITOR_DATABASE_URL=postgresql:///stocks_history?host=/home/<you>/.local/share/trading-pg18/socket&port=5433
```

The editor database must be running before the app serves `/patterns`; if it is
not, the tab reports `PostgreSQL unavailable` and the catalog is empty (there is
no file/SQLite fallback).

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

## Provider balance

The Patterns tab (web) and the Patterns dialog (desktop) show the DeepSeek
account balance in the AI-edit panel, so a submission cannot run out of credit
unexpectedly:

- Source: `GET /user/balance` via the provider adapter (`DeepSeekProvider.balance`),
  cached for 30 seconds and bounded by the same timeout/retry limits as generation.
- Refresh: on load and again when an edit job reaches a terminal state (a
  generation consumes credit).
- Display: `Provider: <model> · Balance: <CUR> <total>` per currency; when the
  provider reports no funds (`is_available: false`) or the total is zero, the
  line is shown in red with `INSUFFICIENT — top up`. A missing key or a failed
  balance call shows `not configured`/`unavailable` and never blocks the editor,
  and the API key is never sent to the client.

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

## Candidate sandbox prerequisites

Generated code executes only inside the sandbox (`core/pattern_edit_worker.py`).
It needs Bubblewrap, `prlimit`, a writable delegated cgroup subtree, and the
worker Python. On a host where the app runs as a systemd service:

```bash
sudo apt-get install -y bubblewrap          # util-linux provides prlimit
sudo loginctl enable-linger <service-user>  # keep the user session cgroup after logout
scripts/sandbox.sh                          # creates the delegated subtree, prints the env values
```

Then set the two printed values in `.env`
(`PATTERN_EDIT_CGROUP_ROOT`, `PATTERN_EDIT_WORKER_PYTHON`) and restart.

On Ubuntu 24.04+, unprivileged user namespaces are restricted by AppArmor and
Bubblewrap fails with `loopback: Failed RTM_NEWADDR: Operation not permitted`.
Allow it for bwrap only, rather than relaxing the global sysctl:

```bash
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

The sandbox cannot run without these; an unavailable sandbox blocks edits
(`blocked`) rather than falling back to unrestricted execution.

## Runtime compatibility failures

Every version records the runtime it was created under: the Python build, the
installed packages, and the trusted engine/interface files (base pattern,
indicator engine, engine defaults, market, backtester, loaders, config — the
full transitive closure, ~26 files).

Execution enforces the Python build and re-checks the version's own captured
non-pattern files against the working tree. The installed-package list is
recorded for audit only — a dependency upgrade does not change the detector
interface, and every candidate is validated against the current environment in
the sandbox anyway. (Enforcing the package list made a single `pip install`
invalid every version, baselines included.)

Consequences you should expect:

- Editing a detector/document pair does **not** invalidate versions.
- Changing one of the captured trusted files (`config.py`, `core/*`, `data/*`,
  `analysis/*`, `utils/*`) does: runs then report `runtime-incompatible` /
  "restore its runtime before execution".
- Because the import batch is immutable, a changed trusted file **cannot be
  re-imported into the same catalog**: `scripts/setup_editor_db.py` reports the
  snapshot difference instead. Recreate the catalog (new database or schema,
  then migrate + import) when you want the baselines to match.
- Generated versions record the runtime they were generated under, so a fresh
  submission after such a change is valid; older ones inherit their base's
  fingerprint and report the incompatibility.

If the captured-file set feels too broad for your workflow, the fix is to narrow
it to the pattern interface; that is a deliberate change with its own tests, not
a configuration toggle.

## Tests

```bash
PATTERN_EDITOR_TEST_DATABASE_URL=postgresql:///pattern_editor_test?host=/tmp/sock&port=55484 \
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ DISPLAY=:1 \
.venv/bin/python -m pytest -q core/test_patterns_web.py core/test_patterns_desktop.py
```

Tests create a random `pattern_editor_test_<uuid>` schema and drop only that
schema. With no test DSN the integration tests report skips, which do not
satisfy any gate.
