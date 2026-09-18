# Pattern Editor phased implementation plan

Source requirements: [pattern-editor.md](pattern-editor.md).

Status: Phases 1–3 complete; Phases 4–11 have not started. Implementation checkboxes reflect verified work only. Proposed new filenames and commands below are implementation targets, not existing capabilities.

## Resume protocol

1. Read the source requirements, this tracker, and applicable repository instructions. Inspect the working tree before changing files; preserve unrelated work.
2. Start at the first incomplete phase whose dependencies are complete. Read its checkpoint, then verify recorded commits, files, migrations, and test evidence against the current checkout.
3. Set the phase to `in progress`. Complete tasks using their stable IDs. Check a task only when its deliverable and relevant verification are finished.
4. Before pausing, update the phase checkpoint and overall tracker with completed task count, exact next task, changed files/commit, verification commands and results, blockers, and any database migration/import/run IDs. Never record credentials.
5. Mark a phase `complete` only when its exit gate passes. An unavailable dependency or skipped test is not a passing check; record it as blocked or pending.
6. If requirements or implementation invalidate a completed task, reopen it and any affected downstream gates. Record the reason in the work log.

Allowed status values: `not started`, `in progress`, `blocked`, `complete`. Counts below refer to task checkboxes, not estimated effort. Resuming this implementation plan is distinct from resuming a running backtest; interrupted backtests may require a new attempt.

## Overall progress tracker

| Phase | Deliverable | Dependencies | Status | Tasks complete |
| --- | --- | --- | --- | --- |
| P01 | Contracts and verification baseline | None | complete | 5/5 |
| P02 | PostgreSQL schema and repository | P01 | complete | 7/7 |
| P03 | Exact file bootstrap and database loader | P02 | complete | 8/8 |
| P04 | Version lifecycle and consumer pinning | P03 | complete | 7/7 |
| P05 | Shared contracts, presets, and durable jobs | P04 | not started | 0/6 |
| P06 | Isolated historical stream backtests | P05 | not started | 0/8 |
| P07 | Stream backtests in both existing frontends | P06 | not started | 0/6 |
| P08 | DeepSeek edit and automatic evaluation pipeline | P07 | not started | 0/8 |
| P09 | Web Patterns tab | P08 | not started | 0/6 |
| P10 | Desktop Patterns dialog | P09 | not started | 0/6 |
| P11 | Integration, recovery, and release readiness | P10 | not started | 0/7 |

**Current resume point:** P05-01. P04 version lifecycle, default/archive operations, and consumer version pinning are complete. Normal runtime discovery and execution now require a configured, migrated PostgreSQL catalog. Publication was rehearsed only in disposable PostgreSQL databases; production migration, import, and consumer cutover have not been performed.

## Requirements that apply to every phase

- PostgreSQL is the only durable editor store, including source, manifests, jobs, presets, reports, and results. No SQLite implementation, fallback, or SQLite integration-test substitute. Existing history-provider behavior is not replaced as part of editor storage work.
- Initial version 1/defaults come from the frozen actual file bytes, including uncommitted changes. No AI rewriting or formatting during import. Import all ten current detector/document pairs, preserving six available and four skipped detectors; verify this inventory again before implementation.
- Pattern helpers must be versioned where required. Trusted engine/interface code stays application code with compatibility checks. A database outage, missing blob, or runtime mismatch must not silently load a different file or version.
- Generated code executes through the existing isolation boundary. An unavailable sandbox blocks execution; no unrestricted fallback.
- Versions are immutable. Deletion archives; default replacement is explicit and atomic. New AI versions never automatically become defaults. Running sessions keep pinned version IDs.
- Both frontends support historical stream backtests. AI edits automatically use a saved preset and compare against the base on identical frozen inputs.
- Use DeepSeek directly with the source document's verified API model ID `deepseek-flash`; verify API compatibility when implementing the adapter and retain provider/model provenance.

## P01 — Contracts and verification baseline

**Status:** complete · **Progress:** 5/5 · **Depends on:** none

**Scope:** Establish concrete interfaces and an honest inventory before replacing storage or execution paths.

- [x] **P01-01:** Audit `core/pattern_edit_store.py`, `core/pattern_versions.py`, loaders, validators, worker pools, scanner, backtester, and both frontends. Record all SQLite, file-discovery, source-mirror, and active-version dependencies that must change.
- [x] **P01-02:** Reconfirm the ten detector IDs/document pairs and their skip/disabled states against the source document. Map each detector's required helpers, dynamic imports, runtime dependencies, and existing fixtures.
- [x] **P01-03:** Define shared typed contracts for pattern/version, import manifest, lifecycle state, preset, edit request, backtest request, progress, result, and domain errors. Specify which fields are immutable and which operations accept expected generations/idempotency keys.
- [x] **P01-04:** Identify the PostgreSQL test configuration and sandbox prerequisites without changing production data. Establish a baseline run of relevant existing tests; distinguish pre-existing failures from missing infrastructure.
- [x] **P01-05:** Record implementation decisions for schema migration ownership, sandbox batching, job recovery, replay isolation, and preset validation. Preserve source-document scope; standalone CLI `--backtest --stream` support remains optional.

**Exit gate:** Consumer/dependency inventory and contract decisions are recorded, with a reproducible baseline test report and explicitly identified missing fixtures/infrastructure.

**Checkpoint:** Last completed task: P01-05. Next: P02-01. Changed files (uncommitted): `core/pattern_editor_contracts.py`, `core/test_pattern_editor_contracts.py`, `scripts/audit_pattern_editor_baseline.py`, `docs/pattern-editor-phase-1-audit.md`, `docs/verification/pattern-editor-p01-inventory.json`, `docs/verification/pattern-editor-p01-baseline-tests.txt`, and this tracker. Baseline commit: `7f343fa7c879243e8fc78fd119a504712a9918ca`. Verification: E01–E04 below. Exit gate met: contracts, consumer/dependency inventory, reproducible test baseline, infrastructure/fixture gaps, and implementation decisions recorded. No P01 blockers. Later prerequisites: dedicated PostgreSQL test DSN (not configured; local socket readiness probe returned no response), configured delegated sandbox, and missing per-detector parity fixtures. Existing baseline failures are recorded, not fixed or counted as passes. See the [audit/handoff](pattern-editor-phase-1-audit.md).

## P02 — PostgreSQL schema and repository

**Status:** complete · **Progress:** 7/7 · **Depends on:** P01

**Primary files:** `core/pattern_edit_store.py`, `config.py`, `.env.example`, new versioned migrations under the selected migration mechanism. Reuse connection conventions from `data/db.py` without coupling editor setup to market-data schema initialization.

- [x] **P02-01:** Implement ordered migrations for a dedicated `pattern_editor` schema with schema-version tracking and an advisory migration lock. Separate migration execution from ordinary reads/application startup.
- [x] **P02-02:** Add patterns, immutable versions, `BYTEA` content blobs, lifecycle state, import batches/manifests/reports, audit events, presets, edit/backtest jobs, reports/results, and worker/lease records needed by the contracts. Use `JSONB` and `TIMESTAMPTZ` appropriately.
- [x] **P02-03:** Enforce foreign keys, immutable source/version payloads, unique pattern/version numbers, idempotency keys, and valid same-pattern defaults. Permit unpublished staging records without exposing an incomplete catalog.
- [x] **P02-04:** Replace SQLite repository operations with PostgreSQL transactions and process-local connections. Allocate version numbers under row locks; implement optimistic generation checks and bounded connection/error handling.
- [x] **P02-05:** Store and retrieve exact artifact bytes with digest/size checks. Make any sandbox materializations disposable; no filesystem registry or artifact directory is authoritative.
- [x] **P02-06:** Add PostgreSQL integration fixtures with isolated schemas/databases and cleanup. Port store-dependent tests; verify uniqueness, rollback, immutable records, concurrent updates, and corrupt/missing blobs.
- [x] **P02-07:** Verify missing configuration/outages return actionable errors and never create/open SQLite or silently initialize a replacement file store. Record migration, backup, and restore commands for the chosen tooling.

**Exit gate:** Repository integration tests pass against PostgreSQL, migration reruns are safe, and the replacement store has no SQLite execution path. Consumer cutover remains P04.

**Checkpoint:** Last completed task: P02-07. Next: P03-01. Changes are uncommitted: PostgreSQL connection/migration support in `core/pattern_editor_db.py`, `migrations/pattern_editor/0001_registry.sql`, `scripts/migrate_pattern_editor.py`; replacement `EditStore`; config/example settings; PostgreSQL fixtures/tests; minimal snapshot/worker compatibility adapters; [operations/handoff](pattern-editor-postgresql.md). Evidence: E05–E07. PostgreSQL 17.11 private test cluster/database only, migration 1; random test schemas cleaned and server stopped. 47 focused tests passed, none skipped; migration CLI applied once and reran as a no-op. No production migration/import/publication performed. P03 bootstrap and P04 consumer cutover remain pending. The old source-mirror recovery path was disabled early to prevent filesystem writes through the replacement repository; verified activation eligibility and UI services remain P04. No P02 blocker; future runs need their own explicit disposable PostgreSQL test DSN. Candidate sandbox configuration remains a later prerequisite.

## P03 — Exact file bootstrap and database loader

**Status:** complete · **Progress:** 8/8 · **Depends on:** P02

**Primary files:** `core/pattern_versions.py`, `core/pattern_loader.py`, `core/pattern_edit_validation.py`, new bootstrap command such as `scripts/import_pattern_baselines.py`, import/parity tests.

Build the explicit database loader here so verification can exercise it before the default consumer path changes in P04.

- [x] **P03-01:** Implement a trusted file-only inventory path that bypasses database overrides and includes skipped/disabled numbered detectors. Capture actual class identity, name, metadata, and paired documentation; reject ambiguous/missing entries.
- [x] **P03-02:** Freeze working-tree bytes and transitive dependencies; record paths, lengths, hashes, Git commit and dirty state, runtime/package identity, and configured availability. Detect source mutation during collection. Resolve dynamic imports explicitly.
- [x] **P03-03:** Stage byte-exact source/document/helper blobs and immutable version-1 records in PostgreSQL with `file-import` provenance and no parent. Import all ten current patterns without changing skip flags or consulting legacy registry selections.
- [x] **P03-04:** Implement loading from an explicit PostgreSQL version ID, including captured helper imports and runtime compatibility checks. Keep trusted interface identity intact and isolate mutable helper/dedup state between evaluations.
- [x] **P03-05:** Read every blob back and compare bytes, lengths, and hashes against the snapshot. Store a complete inventory/verification report. Make identical reruns idempotent and changed inputs explicit conflicts.
- [x] **P03-06:** Add missing triggering/non-triggering fixtures for each available detector. Compare frozen file and database loads in clean workers: metadata, normalized signals, and offline backtest trades/accounting with identical inputs. Verify skipped detectors by bytes/metadata and exclusion rather than enabling them. Ignore only generated provenance IDs and document numeric tolerances.
- [x] **P03-07:** Publish the complete catalog/default set atomically only after all checks pass. Record import batch and verification IDs; failed/incomplete imports remain unpublished and safely retryable. Keep long-running checks outside the final publication transaction while verifying the same immutable batch.
- [x] **P03-08:** Test changed-during-import files, missing dependencies/docs, corruption, partial failures, concurrent bootstrap attempts, reruns, and changed post-bootstrap sources. Prove original pattern files remain unchanged.

**Exit gate:** All ten baseline versions are accounted for in a test PostgreSQL catalog, preserve availability and exact bytes, and all available patterns pass behavioral parity. Initial defaults are exclusively those file-derived baselines. No incomplete import is discoverable.

**Checkpoint:** Last completed task: P03-08. Next: P04-01. Changes are uncommitted: `core/pattern_bootstrap.py`, `core/pattern_bootstrap_parity.py`, `core/pattern_loader.py`, `core/pattern_versions.py`, `core/test_pattern_bootstrap.py`, `scripts/import_pattern_baselines.py`, two synthetic fixtures, [operations/handoff](pattern-editor-bootstrap.md), and verification exports. E08–E10: 59 tests passed across the final suite and two added publication tests, no skips. Actual CLI import and identical rerun passed in PostgreSQL 17.11, disposable `pattern_editor_test` database only: ten published version-1 records, six enabled/four skipped, 44 exact captured files per version. Batch `de4263e88f084c538ff78f74cbd7c214`; report `635d8d7adf264bbd8189ea91f31540a4`; snapshot `ec429bff0fcb51b14fb7b49772c306b1d73c4276e447e4d8fc210ea11adfa1f5`. Migration 1; no new migration required. Test schemas cleaned; private server stopped after verification. Original detector/helper/document files unchanged. No P03 blockers. Normal consumer discovery/worker pinning remains P04; production migration/import/cutover has not been performed.

## P04 — Version lifecycle and consumer pinning

**Status:** complete · **Progress:** 7/7 · **Depends on:** P03

**Primary files:** `core/pattern_loader.py`, `core/pattern_versions.py`, `core/pattern_jobs.py`, `core/scanner.py`, `core/backtester.py`, `core/pattern_provenance.py`, `core/paper_trader.py`, `analysis/chart_renderer.py`, `web/services.py`, `ui/app.py`.

- [x] **P04-01:** Replace normal file enumeration, SQLite existence checks, and source-mirror activation/recovery with published PostgreSQL catalog reads and default-pointer operations. Empty catalogs require explicit bootstrap.
- [x] **P04-02:** Implement version list/detail/source/diff services with validation/backtest/lifecycle state; retain immutable base/parent lineage and monotonic version numbering.
- [x] **P04-03:** Implement explicit default selection with generation checks. Require successful validation/backtest for generated versions; recognize verified import baselines as initially eligible without a fictitious AI job.
- [x] **P04-04:** Implement archive/delete semantics, including atomic default replacement, last-usable-version protection, and rejection of archived versions for new runs. Preserve historical and already pinned reads.
- [x] **P04-05:** Resolve complete version sets once at run/session startup and propagate IDs to inline and spawned workers. Use process-local database access and ensure workers never independently re-resolve current defaults.
- [x] **P04-06:** Preserve signal/trade/position version provenance and define the explicit restart/reload boundary for paper sessions. Move worker acknowledgements/audit writes to PostgreSQL.
- [x] **P04-07:** Test concurrent default/archive/create operations, default changes during active jobs, archived pinned versions, and discovery/execution without original detector files. Audit every P01 consumer for remaining fallback behavior.

**Exit gate:** All runtime consumers use PostgreSQL, default/archive operations preserve invariants, and in-flight runs keep their exact version sets. File-derived parity remains green after cutover.

**Checkpoint:** Last completed task: P04-07. Next: P05-01. Changes are uncommitted: lifecycle services in `core/pattern_versions.py`; PostgreSQL-only discovery/pinning in `core/pattern_loader.py`, `core/pattern_jobs.py`, `core/scanner.py`, `core/backtester.py`; provenance/annotation cutover in `core/pattern_provenance.py`, `analysis/chart_renderer.py`; catalog-based paper cap lookup in `core/paper_trader.py`; shared discovery in `scripts/compare_patterns.py`; consumer/lifecycle tests in `core/test_pattern_lifecycle.py`; updated consumer suites; [operations/handoff](pattern-editor-lifecycle.md) and [test output](verification/pattern-editor-p04-tests.txt). Evidence: E11. Disposable PostgreSQL 17.11 only (reused private test cluster, database `pattern_editor_test`); migration 1; no new migration. 179 focused tests passed across the bootstrap/store/version/loader/validation/contracts/lifecycle and consumer suites, no skips or failures. Two gate regressions fixed: detector-independent trusted-runtime fingerprint (`collect_sources(prune_package=...)`), and `_core_backtest_symbol` resetting the `_dedup` walk registry. Randomized test schemas cleaned; private server stopped. No production migration/import/rollout performed. No P04 blocker. P05 shared service/preset/durable-job adapters remain; the candidate sandbox and live provider stay later gates.

## P05 — Shared contracts, presets, and durable jobs

**Status:** not started · **Progress:** 0/6 · **Depends on:** P04

**Primary files:** new `core/backtest_service.py` and shared schema/job modules; `web/jobs.py`, `ui/backtest_dialog.py`, PostgreSQL repository.

- [ ] **P05-01:** Move shared parameter definitions/validation out of the Tkinter module. Define offline and historical-stream requests with explicit version IDs, market, symbols, timeframe, dates/session count, warmup, capital, sizing, costs, slippage, gates, and end policy.
- [ ] **P05-02:** Persist editable saved presets, freezing an immutable effective preset and resolved universe on each run. Reject unsupported/contradictory inputs; do not silently replace user values.
- [ ] **P05-03:** Implement durable per-run job/status/result interfaces with transactional claiming, leases/heartbeats, bounded concurrency, cancellation, ownership checks, and traceable retry attempts. Keep slow work outside database transactions.
- [ ] **P05-04:** Define frozen dataset storage and result schemas covering signals, trades, equity, drawdown, realized/unrealized P&L, fees, open positions, provenance, and engine/runtime hashes. Store editor-owned artifacts in PostgreSQL; immutable references to existing history must be reproducible, not mutable URLs alone.
- [ ] **P05-05:** Adapt offline backtests to the shared service without changing their documented execution/end-of-data semantics. Remove the single global result-slot assumption while preserving existing frontend behavior until P07.
- [ ] **P05-06:** Test preset freezing, independent concurrent jobs, duplicate requests, stale leases, cancellation races, restart/interruption reporting, and result persistence. Ensure retries cannot publish duplicate attempts/results unintentionally.

**Exit gate:** Both future adapters and AI orchestration have one typed, durable service contract; offline regression tests pass and jobs survive application restarts as records with truthful states.

**Checkpoint:** Last completed task: none. Next: P05-01 after P04. Changed files/commit: none. Verification/evidence: none. Job/run IDs: none. Blockers: none recorded.

## P06 — Isolated historical stream backtests

**Status:** not started · **Progress:** 0/8 · **Depends on:** P05

**Primary files:** shared backtest service, `data/stream_client.py`, `data/stream_server.py`, `core/scanner.py`, `core/paper_trader.py`, `core/paper_books.py`, parity/replay tests.

- [ ] **P06-01:** Extract/reuse paper execution ordering for position management, signal analysis, deferred entries, fills, and accounting. Document the shared path so replay does not grow a separate trading implementation.
- [ ] **P06-02:** Isolate each replay's stream endpoint/session, clock, account, workers, dedup state, and outputs. Parameterize fixed endpoint assumptions; never attach a backtest to an active paper stream or ledger.
- [ ] **P06-03:** Freeze daily historical inputs and warmup. Expose only bars/derived aggregates available at each simulated timestamp; suppress pre-start trading. Enforce supported timeframes and date/session bounds.
- [ ] **P06-04:** Advance only after all current-session work completes. Handle calendars, missing/duplicate bars, unequal histories, data/provider errors, and end-of-data with explicit statuses.
- [ ] **P06-05:** Apply effective sizing, capital, costs, slippage, gates, and version set. Preserve open positions by default; implement optional labeled force-close behavior and consistent persisted metrics.
- [ ] **P06-06:** Integrate progress, cancellation and process cleanup with durable jobs. On restart mark interrupted work truthfully; do not claim resumable execution without saved clock/account state.
- [ ] **P06-07:** Extend paper/backtest parity fixtures to the complete stream/scanner path: signals, pending entries, deduplication, fills, quantities, exits, fees, and P&L. Add future-bar mutation, warmup, calendar, and open-position checks.
- [ ] **P06-08:** Run simultaneous replay jobs alongside an isolated paper-session fixture and prove no interference. Measure representative replay throughput and record sandbox batching/resource decisions and results.

**Exit gate:** Deterministic stream parity, no-lookahead, isolation, and cancellation tests pass. Results identify the exact versions, data, engine, and effective settings.

**Checkpoint:** Last completed task: none. Next: P06-01 after P05. Changed files/commit: none. Verification/evidence and benchmark: none. Run IDs: none. Blockers: none recorded.

## P07 — Stream backtests in both existing frontends

**Status:** not started · **Progress:** 0/6 · **Depends on:** P06

**Primary files:** `web/app.py`, `web/jobs.py`, `web/templates/backtest.html`, relevant web scripts, `ui/backtest_dialog.py`, `main.py`.

- [ ] **P07-01:** Add web mode/version/preset/replay controls and shared validation; launch/query/cancel durable runs by ID through authenticated endpoints.
- [ ] **P07-02:** Add equivalent desktop controls using the same service contract. Run work off the Tk thread and marshal progress/results safely to the main thread.
- [ ] **P07-03:** Display trades, equity, metrics, open positions, errors, and interruption/cancellation states consistently. Distinguish zero trades from failures and offline from stream end policies.
- [ ] **P07-04:** Audit `main.py --web` and `main.py --ui` history-provider initialization, shutdown, and job reconnection. Prove headless web imports do not require Tkinter UI modules.
- [ ] **P07-05:** Add frontend contract tests for malformed settings, unavailable PostgreSQL/data, version selection, preset reuse, cancellation, and persisted results after reopening.
- [ ] **P07-06:** Smoke-test both actual launch modes with the same preset/version; compare effective requests and results. Record exact commands, run IDs, screenshots or observation notes, and any infrastructure blockers.

**Exit gate:** Users can start, inspect, cancel, and revisit stream backtests from both launch modes. This gate is required before AI auto-backtesting work.

**Checkpoint:** Last completed task: none. Next: P07-01 after P06. Changed files/commit: none. Verification/evidence: none. Run IDs: none. Blockers: none recorded.

## P08 — DeepSeek editing and automatic evaluation

**Status:** not started · **Progress:** 0/8 · **Depends on:** P07

**Primary files:** new provider adapter under `ai/providers/`, edit coordinator/service, `config.py`, `.env.example`, existing validator/sandbox modules, PostgreSQL jobs.

- [ ] **P08-01:** Implement the direct DeepSeek adapter for `deepseek-flash`, verifying current official API compatibility. Keep credentials server-side and configurable; bound timeouts, retries, output size, and concurrency. Record requested/returned model metadata.
- [ ] **P08-02:** Build the request from the selected immutable source/docs, required context, interface contract, and instruction. Require structured source/docs/explanation output and restrict editable paths to the chosen pattern pair.
- [ ] **P08-03:** Persist submission and pinned base/preset with an idempotency key before generation. Coordinate durable `queued → generating → validating → backtesting → completed` states and explicit failed/cancelled/blocked outcomes.
- [ ] **P08-04:** Persist usable generated code as exactly one immutable version and source diff before validation. Preserve failed versions and diagnostics; malformed responses without usable code retain a request error without a runnable version.
- [ ] **P08-05:** Adapt isolated validation for PostgreSQL materializations; validate identity, interface, allowed imports/paths, signal fields, and regressions. Test unavailable sandbox, resource exhaustion, disallowed writes/network, and corrupted inputs.
- [ ] **P08-06:** Automatically backtest validated versions with the selected frozen preset/data. Run the base on identical inputs, reusing a result only when version, dataset, engine/runtime, and every effective setting match.
- [ ] **P08-07:** Implement cancellation/recovery between each stage, including races after provider success or version creation. Permit backtest-only retry without another provider call/version. Never auto-promote or silently substitute a model.
- [ ] **P08-08:** Test the entire pipeline with deterministic provider doubles and real PostgreSQL/sandbox execution. Include duplicate Submit, concurrent default changes, malformed responses, provider timeouts, missing data, zero trades, and backtest failure. Record a bounded live-provider smoke result when configured; distinguish it from mocked verification.

**Exit gate:** One valid submission produces one immutable version and automatic comparable backtests. Failure paths preserve auditability, retry safely, and leave defaults unchanged.

**Checkpoint:** Last completed task: none. Next: P08-01 after P07. Changed files/commit: none. Verification/evidence: none. Edit/version/report IDs: none. Live-provider verification: pending. Blockers: none recorded.

## P09 — Web Patterns tab

**Status:** not started · **Progress:** 0/6 · **Depends on:** P08

**Primary files:** `web/app.py`, `web/templates/base.html`, new `web/templates/patterns.html` and client script, web tests.

- [ ] **P09-01:** Add authenticated `/patterns` navigation immediately beside Kronos and API operations for pattern/version details, source/diff, presets, jobs, runs, default selection, and archival. Apply existing mutation protections and domain-error mapping.
- [ ] **P09-02:** Build pattern/version selection, availability/default badges, source/document views, history, and validation/backtest summaries.
- [ ] **P09-03:** Add instruction textbox, visible saved preset selection/configuration, and Submit. Require a configured preset; retain the returned job ID and disable accidental duplicate submission without relying solely on UI controls.
- [ ] **P09-04:** Show generation/validation/backtest progress, explanation/diff, diagnostics, candidate/base results, and cancellation. Reconnect after reload to durable job/run records.
- [ ] **P09-05:** Implement Set default, Backtest again, Edit from this version, and Delete version with eligibility checks, generation-conflict handling, and atomic default replacement.
- [ ] **P09-06:** Add API/browser integration coverage and smoke-test the full workflow, including failed validation, archived versions, expired jobs, zero trades, and page reload during work.

**Exit gate:** The complete Patterns workflow is usable from the web tab; API checks enforce invariants independently of disabled buttons.

**Checkpoint:** Last completed task: none. Next: P09-01 after P08. Changed files/commit: none. Verification/evidence: none. Job/run IDs: none. Blockers: none recorded.

## P10 — Desktop Patterns dialog

**Status:** not started · **Progress:** 0/6 · **Depends on:** P09

**Primary files:** `ui/app.py`, new `ui/patterns_dialog.py`, shared services and desktop tests.

- [ ] **P10-01:** Add a Patterns toolbar button beside Kronos that opens the editor dialog and uses shared services rather than implementing separate business rules.
- [ ] **P10-02:** Add pattern/version history, default/availability badges, source/docs/diff, and saved preset configuration/selection.
- [ ] **P10-03:** Add instruction textbox and Submit with pinned selections and durable job IDs. Keep provider, validation, and backtesting work off the UI thread.
- [ ] **P10-04:** Display progress, errors, candidate/base results, and cancellation; reconnect to durable jobs after closing/reopening. Closing a window must not falsely mark a job cancelled or completed.
- [ ] **P10-05:** Wire default selection, archival/replacement, edit-from-version, and backtest retry with the same conflict and eligibility behavior as web.
- [ ] **P10-06:** Exercise the full workflow under `main.py --ui`, including thread-safe updates, window closure during a job, PostgreSQL errors, and defaults changed from web while desktop is open.

**Exit gate:** Desktop and web have equivalent supported actions and enforce the same backend rules; the desktop remains responsive during execution.

**Checkpoint:** Last completed task: none. Next: P10-01 after P09. Changed files/commit: none. Verification/evidence: none. Job/run IDs: none. Blockers: none recorded.

## P11 — Integration, recovery, and release readiness

**Status:** not started · **Progress:** 0/7 · **Depends on:** P10

**Primary files:** integration suites, operator documentation, deployment/configuration references, both planning documents as needed to reflect implemented decisions.

- [ ] **P11-01:** Run required pattern/store/loader/validation/worker, accounting, paper/backtest parity, web, and desktop checks against the final implementation. Record exact commands, environment prerequisites, results, and any unresolved failures.
- [ ] **P11-02:** Rehearse migration/bootstrap on an isolated PostgreSQL environment using the intended release's actual source snapshot. Verify every byte/hash, all pattern availability states, parity reports, initial defaults, reruns, and failed-publication recovery.
- [ ] **P11-03:** Rehearse PostgreSQL backup/restore and application restart during generation, validation, and replay. Confirm interrupted jobs remain visible, retries are traceable, and no SQLite/file fallback occurs.
- [ ] **P11-04:** Run concurrent web/desktop edits, default/archive conflicts, two stream backtests, and a separate paper-session fixture. Check version pinning, account isolation, cancellation cleanup, and historical result readability.
- [ ] **P11-05:** Document setup, migrations, exact import/verification commands, sandbox prerequisites, DeepSeek configuration, first-preset setup, default/delete semantics, job retry, backup/restore, and runtime compatibility failures. Keep credentials out of examples/evidence.
- [ ] **P11-06:** Prepare the concrete cutover checklist: verified source snapshot and import report, PostgreSQL backup, old worker shutdown/new worker startup boundaries, health checks for both launch modes, and rollback conditions. Apply to the target environment only within separately established implementation/deployment authorization; document actual rollout status accurately.
- [ ] **P11-07:** Review every acceptance criterion in `pattern-editor.md`, link it to passing evidence or an explicit blocker, reconcile all phase trackers, and record remaining operational limitations. Do not claim deployment from a rehearsal alone.

**Exit gate:** Source-document acceptance criteria have evidence, no required verification is silently skipped, and an executable recovery/cutover procedure exists. Actual deployment status is separately recorded.

**Checkpoint:** Last completed task: none. Next: P11-01 after P10. Changed files/commit: none. Verification/evidence: none. Import/backup/run IDs: none. Rollout status: not performed. Blockers: none recorded.

## Verification evidence index

Populate as work proceeds. Store durable import/job/backtest reports in PostgreSQL; repository notes may link report IDs and exported test output without becoming authoritative runtime storage.

| Evidence ID | Phase/task | Command or scenario | Result | Commit/environment | Report/artifact reference |
| --- | --- | --- | --- | --- | --- |
| E01 | P01-01, P01-02 | Consumer audit and `scripts/audit_pattern_editor_baseline.py` with fixture Settings environment | Ten detectors/document pairs; six available/four skipped; hashes and dependency map recorded | Baseline commit above; working-tree audit | [Audit](pattern-editor-phase-1-audit.md), [inventory](verification/pattern-editor-p01-inventory.json) |
| E02 | P01-04 | Nine-suite pytest command in an isolated baseline archive; exact command in audit | 58 passed, 2 existing failures, 14 warnings; no skipped tests | Untouched baseline commit; no live registry copied | [Full output](verification/pattern-editor-p01-baseline-tests.txt), failure classifications in audit |
| E03 | P01-03 | `.venv/bin/python -m pytest -q core/test_pattern_editor_contracts.py` | 23 passed | New contracts; no DB access | Contract test suite and audit |
| E04 | P01-04, P01-05 | Package/binary checks, read-only `CandidateRunner.available()`, `pg_isready -h /var/run/postgresql -p 5432 -t 2` | Dependencies found; sandbox configuration missing; PostgreSQL probe exit 2; no test DSN configured | Current execution context; no production data changes | Infrastructure observations and decisions in audit |
| E05 | P02-01–P02-06 | PostgreSQL integration/ported storage suites plus contract tests; exact command in operations document | 47 passed, no skips, 3.17s | PostgreSQL 17.11 private test DB; current working tree | [Test output](verification/pattern-editor-p02-tests.txt), [operations](pattern-editor-postgresql.md) |
| E06 | P02-01, P02-07 | `scripts/migrate_pattern_editor.py` twice with explicit disposable editor DSN | Applied migration 1; already up to date on rerun | Private DB only, default schema in smoke test | Operations verification record |
| E07 | P02-07 | Schema cleanup, private server shutdown, `git diff --check`; backup/restore procedure documented | Test schemas removed; no production migration | Private `/tmp` cluster only | Operations/handoff document |
| E08 | P03-01–P03-08 | Full bootstrap/store/version/loader/validation/contracts suite, then two added publication/conflict tests; exact commands in P03 handoff | 57 + 2 passed, no skips; all six available detectors trigger and pass exact signal/trade/accounting parity; four skipped excluded | PostgreSQL 17.11 private test schemas; working tree | [Test output](verification/pattern-editor-p03-tests.txt), [handoff](pattern-editor-bootstrap.md) |
| E09 | P03-03–P03-07 | Explicit migration, `scripts/import_pattern_baselines.py`, identical CLI rerun, read-only catalog queries | Same batch/report/defaults on rerun; ten version-1 defaults, six enabled; complete verification report | Disposable test database only; migration 1 | [CLI output](verification/pattern-editor-p03-import.txt), [report export](verification/pattern-editor-p03-report.json) |
| E10 | P03-08 | Test-schema cleanup query, source-file diff, compile check, `git diff --check`, private server shutdown | No test schemas remain; no original pattern file changes; checks pass | Private `/tmp/pattern-editor-p03-pg` cluster only | P03 handoff |
| E11 | P04-01–P04-07 | Nineteen-file editor/consumer gate against disposable PostgreSQL; two regression fixes (`collect_sources(prune_package=...)`, `_core_backtest_symbol` dedup reset) | 179 passed, 0 failed, 0 skipped; source-less discovery/execution, lifecycle invariants, pinning and concurrency verified | PostgreSQL 17.11 private test DB; migration 1 | [Test output](verification/pattern-editor-p04-tests.txt), [handoff](pattern-editor-lifecycle.md) |

Suggested existing suites to incorporate after adapting storage fixtures: `core/test_pattern_versions.py`, `core/test_pattern_loader.py`, `core/test_pattern_edit_validation.py`, `core/test_pattern_jobs.py`, `tests/test_backtest_paper_parity.py`, `web/test_services_patterns.py`, and relevant scanner/accounting/replay tests. Record the actual selected commands and results; these suggestions are not claims that the suites already cover the new behavior.

## Work and decision log

Append a row after each implementation session or material decision. Update the relevant phase checkpoint as well; the log does not replace task checkboxes.

| Date | Phase/tasks | Completed work or decision | Verification | Next action / blocker |
| --- | --- | --- | --- | --- |
| Planning session | Document only | Created resumable phased plan from `pattern-editor.md`; no runtime changes | Document structure/checklist review | Start P01-01 |
| 2026-09-18 | P01-01–P01-05 | Added frozen typed contracts, source inventory/audit tool, dependency/consumer matrix, and explicit migration/replay/recovery decisions; marked P01 complete | E01–E04; 23 new tests pass; baseline 58 pass/2 existing failures | P02-01; configure isolated PostgreSQL test access before P02 integration checks |
| 2026-09-18 | P02-01–P02-07 | Replaced editor store with PostgreSQL, explicit checksummed migrations, immutable blobs/versions and transactional constraints; ported storage tests; documented setup/recovery | E05–E07; 47 passed, no skips; CLI apply/rerun verified | P03-01: exact file bootstrap; runtime consumers still require P04 cutover |
| 2026-09-18 | P03-01–P03-08 | Added exact trusted inventory/freeze, immutable idempotent staging, captured helper/dynamic imports, strict runtime compatibility, clean-worker signal/trade/accounting parity, and atomic verified publication; preserved six available/four skipped | E08–E10; 59 tests passed; actual import/rerun and exported report in disposable PostgreSQL | P04-01: normal consumer discovery and pinned lifecycle cutover; no production publication performed |
| 2026-09-18 | P04-01–P04-07 | Cut every runtime consumer over to published PostgreSQL discovery and default-pointer operations; added version list/detail/source/diff, validation/backtest-gated default selection and idempotent archive with replacement; pinned one version set per run/session into inline and spawned workers; preserved version provenance and moved worker acknowledgements to PostgreSQL; fixed detector-dependent runtime fingerprint and `_dedup` walk-state leak | E11; 179 passed, 0 failed, 0 skipped across editor + consumer gate in disposable PostgreSQL | P05-01: shared typed service, presets and durable jobs; candidate sandbox/live provider remain later gates |
