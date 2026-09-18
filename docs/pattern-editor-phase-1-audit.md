# Pattern Editor Phase 1 audit and contracts

Phase: P01 in [the implementation tracker](pattern-editor-implementation-plan.md). Requirements: [Pattern Editor design](pattern-editor.md). Baseline commit: `7f343fa7c879243e8fc78fd119a504712a9918ca`.

This phase adds storage-independent contracts and records migration/execution dependencies. It does not replace storage, import patterns into a database, change pattern rules, or enable skipped detectors. New contracts are not wired into existing runtime consumers yet.

## P01-01 — Consumer and persistence audit

| Existing location | Observed dependency or behavior | Required change / phase |
| --- | --- | --- |
| `core/pattern_edit_store.py:EditStore` | Constructor creates directories, SQLite schema/WAL, and immutable-version triggers; filesystem blobs, SQLite placeholders/rows, `BEGIN IMMEDIATE`, and `INSERT OR REPLACE` semantics | P02: explicit PostgreSQL migration command and repository; process-local connections; database blobs; no constructor DDL or SQLite fallback |
| `core/pattern_versions.py:baseline`, `pattern_source`, `collect_sources` | Derives ID/path conventions, snapshots files on first edit, reads active SQLite selection, checks disk mirrors, writes manifest files; AST import closure does not resolve dynamic imports | P03: authoritative file-only inventory including skipped patterns, exact bytes, explicit dynamic-dependency resolution, staged PostgreSQL import |
| `core/pattern_versions.py:recover`, `mirror_matches` | Activation recovery overwrites pattern source files before advancing active selection | P04: retire source-mirror journal/recovery; atomic PostgreSQL default pointer and event |
| `core/pattern_loader.py:active_versions`, `acknowledge_worker` | SQLite-file existence controls behavior; worker heartbeat uses SQLite writes | P04: explicit catalog/bootstrap status and PostgreSQL worker records |
| `core/pattern_loader.py:discover` | Enumerates modules even for explicit versions; derives IDs from filenames; falls back to ordinary imports; version branch bypasses normal `instance.skipped` check | P03/P04: database enumeration, exact identity and availability checks, explicit pinned IDs without fallback |
| `core/pattern_loader.py:trusted_baseline`, `VersionPattern` | Private static pattern imports but shared trusted interface; compatibility checks; copied dedup state; generated versions execute through a fresh validator/runner per analysis | P03: verify helper isolation and dynamic import interception; P06/P08: bounded run/batch execution, not unrestricted host execution |
| `core/scanner.py:_scan_all`, `_discover_patterns` | Re-resolves defaults and restarts workers automatically at scan boundaries; records file paths for diagnostics | P04: pin for session lifetime until explicit reload/restart; display version provenance instead of treating filenames as authoritative |
| `core/pattern_jobs.py:make_analyze_pool`, `init_analyze_worker` | Pool independently calls `active_versions()` at creation; can race scanner's already-resolved set | P04: caller passes one pinned set to inline and spawned execution; no inherited DB connections |
| `core/backtester.py:_iter_pattern_classes`, `discover_pattern_names`, `_load_patterns` | UI name enumeration imports files; workers accept both module/class and `version:` specs | P04: database catalog/name enumeration and explicit version loading; legacy file-only helpers limited to bootstrap/tests |
| `core/backtester.py:Backtester` | Constructor discovers current defaults; symbol workers use offline tape and file-derived metadata; full-tape end-margin semantics | P04/P05: explicit version set and input snapshot; preserve labeled offline behavior |
| `core/pattern_edit_validation.py`, `core/pattern_edit_evaluator.py` | Filesystem blob inputs and trusted harness; validation rejects skipped patterns; signal preview/tests, not portfolio replay | P03: dedicated import inspection for skipped records; P08: PostgreSQL materialization and validated candidate execution |
| `core/pattern_edit_worker.py` | Bubblewrap/seccomp/cgroup runner with short per-call limits and filesystem sandbox inputs; no real sandbox integration coverage in validator tests | P06/P08: bounded batching and isolation verification; keep fail-closed behavior |
| `core/pattern_provenance.py`, `patterns/base_pattern.py:TradeSignal` | Version/signal IDs and requested/resolved rules already exist | P04/P05: preserve through account positions, results, retries, and archived historical reads |
| `web/services.py:ChartService`, `ui/app.py` | Cached detector instances discovered on service/window creation | P04: define pinned explorer request/session boundary and explicit refresh; neither silently reads a different version |
| `web/jobs.py`, `ui/backtest_dialog.py` | Web imports Tk form definitions; offline-only constructor parameters; one in-memory web backtest slot | P05/P07: shared contracts and parameter schema, durable independent runs, both stream adapters |
| `web/app.py`, `web/templates/base.html`, `ui/app.py` | Backtest endpoints and Kronos navigation already exist; no complete Patterns editor path | P07/P09/P10: shared service calls, Patterns tab/button, persistent job IDs |
| `data/stream_client.py`, `data/stream_server.py` | Settings-derived stream endpoint and mutable server replay control; advancement protocol is scanner-driven | P06: isolated endpoint/session and clock per replay, session barriers |
| `core/paper_books.py`, `core/paper_trader.py` | Book startup loads saved accounts and can reuse/start a configured stream; account persistence is separate from editor | P06: never construct a replay by attaching to the user's live PaperBook/ledger; isolate account and durable editor results |
| `main.py` | Frontend history initialization differs by launch mode; `--stream` requires `--paper` | P07: verify both frontend launch paths; standalone stream CLI extension remains optional |
| `patterns/_rationale.py:_Ctx.module`, `patterns/_annotations.py`, `analysis/chart_renderer.py` | Rationale uses `importlib.import_module` through `_MODULES`; chart annotation path can re-import mutable detector thresholds | P03/P04: explicit version-aware detector/helper resolver and annotation provenance; static import snapshots alone are insufficient |
| `scripts/compare_patterns.py`, `generate_pattern_demos.py` | Additional file discovery/import utilities outside the primary consumers | P04: audit/update comparison execution; keep demos explicitly developer/file tooling, never production fallback |
| `core/test_pattern_versions.py`, loader/validator tests | Temporary legacy SQLite stores and source-mirror expectations | P02/P03: port storage fixtures to real isolated PostgreSQL; old baseline results do not count as PostgreSQL integration verification |

`web/replay_store.py` stores unrelated replay UI configuration in a JSON file. Do not repurpose it for durable editor jobs/results. `data/db.py` initializes stock-history tables independently; editor migrations must not be hidden in that initializer. No active Python migration files or provider adapter files were present under `migrations/versions/` or `ai/providers/` during this audit (only cache directories).

## P01-02 — Detector, helper, runtime, and fixture inventory

The read-only audit command is `scripts/audit_pattern_editor_baseline.py`. Its checked-in [inventory](verification/pattern-editor-p01-inventory.json) records exact source/document/dependency SHA-256 hashes, sizes, working-tree context, runtime package versions, class metadata, and static transitive repository dependencies for each detector. This is P01 evidence, not the atomic, verified P03 import manifest. Run the command in a fresh process; P03 must additionally resolve dynamic imports and verify behavior before publication.

All entries have timeframe `1d`, paired `.md` files, and no configured disabling (`DISABLED_PATTERNS=[]`). `Horizon` includes the current loader's fallback of 5 when the class supplies none. None of the audit code uses `discover()` or active registry overrides.

| Module (under `patterns/`) | Class | Skipped | Min bars / horizon / max open | Pattern helpers in static closure (excluding package/interface) | Existing fixture evidence / gap |
| --- | --- | --- | --- | --- | --- |
| `002_double_top` | `DoubleTopPattern` | no | 30 / 3 / none | `_dedup`, `_rules` | Golden tape and paper parity: TXN/CDNS; add explicit non-trigger case and database parity |
| `003_double_bottom` | `DoubleBottomPattern` | no | 30 / 5 / none | `_dedup`, `_rules` | Demo CSVs and rationale tests exist; missing proven file/database triggering + non-triggering backtest pair |
| `004_rounding_bottom` | `RoundingBottomPattern` | no | 121 / 5 / none | `_rounding`, `_annotations`, `_rationale`, `_dedup`, `_rules` | Golden tape/paper parity: ON; add non-trigger case and database parity |
| `005_rounding_top` | `RoundingTopPattern` | no | 121 / 5 / none | `_rounding`, `_annotations`, `_rationale`, `_dedup`, `_rules` | Demo CSV exists; no complete detector-specific import parity pair established |
| `006_upward_channel` | `UpwardChannelPattern` | no | 40 / 16 / none | `_channels`, `_dedup`, `_rules` | Demo CSV exists; earnings inputs need freezing; no complete import parity pair established |
| `007_descending_channel` | `DescendingChannelPattern` | yes | 40 / 5 / none | `_channels`, `_dedup`, `_rules` | Demo CSV exists; preserve skipped state, verify bytes/metadata and exclusion |
| `008_head_and_shoulders` | `HeadAndShouldersPattern` | yes | 80 / 3 / none | `_dedup`, `_rules` | `hs_control.csv`, NET/SWKS parity and golden tapes exist; these directly execute a skipped detector and do not authorize enabling it |
| `009_flag_pattern` | `FlagPattern` | yes | 120 / 3 / 1 | `_dedup`, `_rules` | Web test incorrectly expects availability; preserve skipped state |
| `010_pennant` | `PennantPattern` | no | 50 / 25 / 1 | `_dedup` | Overlay tests exist; no complete detector triggering/non-triggering import parity pair established |
| `011_breakout_retest` | `BreakoutRetestPattern` | yes | 105 / 5 / none | `_rules` | Preserve skipped state; verify bytes/metadata and exclusion |

Pattern IDs are `pattern_` plus the displayed module name, confirmed from the actual instantiated trusted classes rather than assumed. Import coverage must remain ten records, six available/four skipped unless the source changes deliberately before P03.

Dependency details:

- `_rationale._MODULES` dynamically points to detector modules or `_channels`; its exact mapping is included in the JSON inventory. In the rounding closure, `_annotations` reaches `_rationale`; renderer rationale for other patterns also reaches these imports. A custom `__import__` wrapper alone does not intercept `importlib.import_module`. P03 needs a version-bound resolver or full isolated module namespace for these paths, tested with mutated host helpers/detectors.
- `_rules` uses EDGAR and lazily imports `data.barcache.load_earnings_cache`; AST traversal finds this lazy import, but hashes alone do not freeze earnings data or configuration. P03/P06 must freeze/disable these external inputs explicitly per preset so comparisons are deterministic.
- Shared trusted runtime closure includes `analysis/indicator_engine.py`, `data/ohlcv_store.py`, `data/tv_client.py`, `config.py`, `utils/logger.py`, and, through `_rules`, history/EDGAR/barcache/market/database modules. The JSON lists the precise closure per pattern. Runtime inventory must distinguish code bytes, effective nonsecret settings, and frozen external data.
- Observed packages: Python `cpython-312`, NumPy `2.5.1`, pandas `2.2.2`, Pydantic `2.13.4`, psycopg `3.3.4`, pytest `9.1.1`; MCP and tradingview-screener versions are recorded in JSON. `tv_client` imports both packages even when only its candle/snapshot classes are used.
- Existing `runtime_manifest()` covers only six trusted files and NumPy/pandas. P03 must extend compatibility evidence to the actual trusted closure without copying runtime secrets into artifacts.
- Demo CSVs and rendering tests are starting material, not evidence of deterministic entry/exit parity. P03 owns missing triggering/non-triggering fixtures; P06 owns full scanner/stream no-lookahead and isolation fixtures.

Reproduce the inventory (no database connection):

```bash
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ .venv/bin/python scripts/audit_pattern_editor_baseline.py > /tmp/pattern-editor-p01-inventory.json
```

## P01-03 — Shared typed contracts

Implemented in [core/pattern_editor_contracts.py](../core/pattern_editor_contracts.py). They import no settings, repository, detector, or UI modules. Pydantic frozen models, nested frozen models, and tuples prevent normal mutation and support JSON round trips and spawned-worker pickling. These models are a transport/domain boundary, not a sandbox or a replacement for database constraints; trusted code must not bypass validation with unchecked construction/update helpers.

| Contract | Semantics / ownership |
| --- | --- |
| `ContentRef`, `SourceFile`, `RuntimeIdentity` | Immutable content-addressed PostgreSQL bytes and normalized relative materialization paths; runtime code/package identity |
| `PatternMetadata`, `PatternRecord` | Exact detector interface metadata; immutable snapshot of a mutable catalog row (enabled/default/generation/publication) |
| `PatternVersion`, `ProviderProvenance` | Immutable lineage, source manifest/hash, model/import origin; failed AI versions may lack validated metadata |
| `ImportEntry`, `ImportManifest` | Frozen file inventory and availability, Git/dirty context, byte-level snapshot identity |
| `VersionLifecycle`, `ValidationState` | Separate mutable database lifecycle exposed as a frozen snapshot; validation report, verified-baseline eligibility, successful run, archive timestamp, generation |
| `ReplayWindow`, `ExecutionSettings`, `BacktestSettings` | Explicit market/universe/symbols, replay bounds/warmup, finite costs/capital, gates/sizing/end policy. No implicit stream date/universe. Daily replay only initially; offline retains full-tape legacy policy |
| `BacktestPreset` | Immutable generation snapshot of an editable saved preset; service checks this against persisted preset at submission |
| `VersionSelection`, `BacktestRequest`, `EditRequest` | Pinned versions/base; submitted preset snapshot, idempotency key, retry parent. One version per pattern; edits require a nonblank instruction and stream preset |
| `DefaultChange`, `ArchiveVersion` | Explicit expected pattern generation plus idempotency key; archive may atomically replace current default |
| `JobState`, `JobProgress` | Attempt/state/progress/error snapshot. Includes `interrupted` distinct from cancellation/failure; no unsupported resume promise |
| `ErrorCode`, `DomainError` | Stable typed failure categories, safe message, retry eligibility, optional report link; never raw DSN/provider secret/worker stderr |
| `FrozenRunInputs`, `BacktestMetrics`, `BacktestResult` | Resolved symbols, dataset/runtime/settings/input hash and completed metrics/artifact references. Detailed trade/result artifact row adapters remain P05 work |

Transaction/service invariants deferred to their assigned phases (not falsely claimed as Pydantic checks):

- P02/P04 enforce same-pattern parent/default/replacement, monotonic numbers, immutable blobs/versions, availability, last-usable-version protection, and eligible validation/backtest evidence. Published enabled patterns require a default; unpublished staging may have none. Source metadata for skipped patterns cannot override availability.
- P03 binds a manifest to byte-verified imports and metadata; verifies source/document membership, hashes, dependency completeness and publication. File-import version 1 has no parent/model job; AI versions have a same-pattern parent and provider/job provenance.
- P05 validates preset generation and canonical symbol resolution, rejects duplicate/unknown effective parameter names, resolves all runtime defaults once, and freezes complete risk/gate settings. The contract permits parameter entries but does not itself know engine-specific names/ranges. Hash canonical JSON with stable ordering, not Python object representations; a stale preset is a conflict rather than silent replacement.
- Edit/backtest creation and default/archive mutations require idempotency keys. Same key + same canonical request returns the original operation; same key + different request is a conflict. Retries have new operation keys and an explicit attempt/parent link. Changing defaults while an edit runs does not rebase that edit.
- Preset updates use `preset_id` plus expected generation; cancellation uses job ID, expected attempt/ownership, and repeat-safe semantics. Worker completion requires the current lease token. Database rows, not the client, determine actor, timestamps, eligibility, and final hashes.
- Candidate and baseline comparison must use the same frozen dataset/effective settings/runtime; each run's identity separately includes its version set. Do not require the two full input hashes to be identical when their version IDs differ.

## P01-04 — Verification baseline and infrastructure

Baseline tests ran against an isolated `git archive` of the commit above under `/tmp/pattern-editor-p01-baseline`, using the project's existing virtualenv. Tracked source had no pre-existing modifications; the planning documents were untracked. The actual application registry and `.env` were not copied, and no production PostgreSQL connection or migration was performed. Temporary legacy SQLite fixtures are evidence of the old implementation only, not a new PostgreSQL test substitute.

Reproduction from the repository (use a new empty directory for each baseline):

```bash
baseline_dir=$(mktemp -d /tmp/pattern-editor-p01.XXXXXX)
project_python="$PWD/.venv/bin/python"
git archive 7f343fa7c879243e8fc78fd119a504712a9918ca | tar -x -C "$baseline_dir"
cd "$baseline_dir"
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ "$project_python" -m pytest -q core/test_pattern_versions.py core/test_pattern_loader.py core/test_pattern_edit_validation.py core/test_pattern_jobs.py tests/test_backtest_paper_parity.py web/test_services_patterns.py core/test_execution_accounting.py core/test_scanner_deferred_entry.py data/test_stream_replay_sync.py
```

Observed result: **58 passed, 2 failed, 14 warnings in 8.86 seconds**. No tests skipped. The initial attempt without the three Settings environment values had eight collection errors; setting the nonsecret fixture values above resolved those environment errors.

| Failure | Classification | Follow-up |
| --- | --- | --- |
| `core/test_pattern_versions.py::test_two_process_baseline` — `sqlite3.OperationalError: database is locked` | Existing concurrent SQLite initialization failure in the untouched baseline; observed once, intermittency not established | P02 replaces SQLite, then verifies concurrent initialization/allocation on PostgreSQL; do not suppress the error or claim it fixed in P01 |
| `web/test_services_patterns.py::DiscoverPatternsDisabledTests::test_default_run_covers_ported_patterns_but_not_retired_011` — expects `pattern_009_flag_pattern` | Existing test expectation contradicts actual `skipped=True` source | P04 update discovery tests to preserve actual availability. Do not enable pattern 009 to satisfy the stale assertion |

New contract tests: `.venv/bin/python -m pytest -q core/test_pattern_editor_contracts.py` — **23 passed**. Cover serialization/pickle stability, nested immutability, path traversal, replay bounds, unsupported input, finite costs, duplicate version selection, and edit preset requirements.

Infrastructure observations and future test configuration:

- `psycopg`, `psql`, and `pg_isready` are installed. `pg_isready -h /var/run/postgresql -p 5432 -t 2` returned exit 2 (`no response`) in this execution context. This does not establish remote PostgreSQL availability.
- No `PATTERN_EDITOR_TEST_DATABASE_URL` was provided. Reserve that explicit variable for P02 integration fixtures; **never fall back to the application's `DATABASE_URL`**. Require a dedicated disposable database and generate a unique schema per test worker, with cleanup constrained to that schema. Test-role DDL permissions must be verified there. No database/schema was created or changed during P01.
- Runtime connection configuration already exists as `settings.database_url` with discrete `db_*` fallbacks. Do not log its value. Keep editor schema qualification explicit; use an explicit deployment DSN if history is API-hosted and the frontend has no local PostgreSQL.
- `/usr/bin/bwrap`, `/usr/bin/prlimit`, `libseccomp.so.2`, and cgroup v2 were found. Read-only `CandidateRunner.available()` failed because `PATTERN_EDIT_CGROUP_ROOT` and/or `PATTERN_EDIT_WORKER_PYTHON` were not configured. Full sandbox execution was **not tested**.
- P06/P08 need a writable delegated cgroup v2 subtree with `pids`, `memory`, and `cpu` controllers, a configured worker Python with required packages, Bubblewrap namespace support, and verified seccomp/limits/cleanup. Presence of binaries alone is not evidence of working isolation. The current error points to missing `docs/pattern-edit-operation.md`; P11 must supply a real operations reference.

These are prerequisites for later phases, not blocked P01 deliverables: P01 requires their identification and an honest baseline, not deploying PostgreSQL or configuring the host sandbox.

## P01-05 — Recorded implementation decisions

1. **Migration ownership (P02):** Add an explicit editor migration command and ordered SQL migrations scoped to `pattern_editor`, with version/checksum tracking and a PostgreSQL advisory lock. No migration framework is currently present in active source, so use the existing psycopg dependency rather than introducing an ORM just for this feature. No schema DDL in ordinary repository reads. Back up before deployment migration; isolate test DSNs/schemas as above.
2. **Bootstrap authority (P03):** Actual working-tree detector/document/helper bytes establish version 1. Legacy SQLite selections are never consulted. Import skipped patterns as unavailable records; do not route them through the normal candidate validator that rejects skipped patterns. Stage and verify outside the final short publication transaction, then atomically publish the immutable batch.
3. **Sandbox batching (P06/P08):** Target one isolated evaluator per bounded replay batch or run, with explicit limits, cancellation, input/output protocol, and private helper state. Start with the existing strict boundary and benchmark before choosing batch size/time limits. Do not reuse a worker across independent jobs or call the provider from inside candidate execution. Existing 20-second CPU/per-call timeout limits cannot simply be assumed adequate for portfolio replay.
4. **Job recovery (P05/P08):** PostgreSQL jobs claimed with row locking/`SKIP LOCKED`, ownership token, attempt, lease expiry, and heartbeat. Persist stage outputs before advancing; late workers cannot overwrite a newer attempt. Mark abandoned work interrupted; explicit retry references the previous attempt. Reuse a persisted generated version after a crash rather than calling AI again. Provider success before local persistence may be ambiguous: report it and use explicit retry policy; do not promise exactly-once provider billing.
5. **Replay isolation (P06):** Each run gets a private replay process/session/endpoint, clock, account, worker/dedup state, and frozen inputs. Share paper execution ordering, not the user's PaperBook instance or saved ledger. Advance on completion of all symbol work for a simulated session; version sets are immutable for the run. Add future-bar mutation and simultaneous-run isolation tests.
6. **Preset validation (P05/P07):** Central typed contracts plus service-level engine validation, used by both frontends and AI jobs. Require a saved preset for automatic edits; reject unknown fields, unsupported timeframes/options, stale generations, ambiguous dates/session counts, and missing symbols/universe. Freeze resolved effective settings and history. Daily stream end policy defaults in the preset UI to keep-open; contracts require an explicit value. Offline mode retains its distinct documented policy.
7. **Scope:** P01 introduces contracts and audit evidence only. P02 owns persistence replacement; P03/P04 own loading/cutover; P05 owns service adapters; later phases own UI/provider wiring. The standalone CLI `--backtest --stream` extension is not required for the two frontend stream flows.

## Handoff

Resume at **P02-01**. Use the inventory as audit evidence, then regenerate it against the actual P03 source snapshot. Required upcoming infrastructure: explicit disposable PostgreSQL test DSN; later a working delegated sandbox. Existing baseline failures and missing per-detector parity fixtures remain tracked work, not completed fixes.
