# Pattern Editor implementation plan

Status: proposed. This document plans the implementation; it does not change runtime behavior.

## Goal

Read patterns exclusively from PostgreSQL, initially seeded byte-for-byte from the repository’s file patterns, maintain immutable versions with a selectable default, and provide a **Patterns** editor beside **Kronos**. A user selects a pattern/version, describes a change in a textbox, and presses **Submit**. AI edits that pattern, creates a version, and automatically backtests it using historical streaming behavior shared with paper trading. Both `python main.py --web` and `python main.py --ui` must support stream backtests.

## Existing code and gaps

- `core/pattern_edit_store.py` currently implements a SQLite registry at `data/pattern_edit/registry.sqlite3`, with source/artifact bytes under `pattern_versions/`. This is legacy implementation to replace, not the proposed storage backend. The new implementation must not create, read, or fall back to SQLite.
- PostgreSQL support already exists through `psycopg` in `requirements.txt`, `settings.database_url`/`DATABASE_URL`, and `data/db.py` connection helpers. Reuse the connection conventions while keeping editor schema migrations separate from market-data schema initialization.
- `core/pattern_versions.py` snapshots file-backed baselines and dependencies. Its activation recovery writes source mirrors into `patterns/`; database authority requires replacing that behavior.
- `core/pattern_loader.py` supports explicit versions, but discovers available patterns by enumerating Python files and falls back to file imports. This does not yet satisfy database-backed discovery/loading.
- `core/pattern_edit_validation.py`, `core/pattern_edit_worker.py`, and `core/pattern_edit_evaluator.py` provide candidate validation and isolated execution. The evaluator currently evaluates signals; it is not a complete portfolio backtest.
- `core/backtester.py` is an offline daily-bar backtester. `web/jobs.py` and `ui/backtest_dialog.py` expose cache-based settings, without a historical stream mode. Web form definitions currently depend on the desktop dialog module.
- `data/stream_client.py`, `data/stream_server.py`, `core/scanner.py`, and `core/paper_trader.py` contain the historical replay and paper execution pieces to reuse.
- `tests/test_backtest_paper_parity.py` checks selected fill scenarios; it does not establish full stream/scanner parity or absence of future-data exposure.
- Web navigation lives in `web/templates/base.html`. Desktop `ui/app.py` currently opens Kronos using a toolbar button, rather than a tab.

## Product decisions

Items 1, 2, 7, 8, and the provider/model choice in item 6 are confirmed by the user. Other lifecycle details are proposed implementation defaults.

1. Provide a web **Patterns** tab immediately beside **Kronos**, plus a desktop **Patterns** toolbar button/dialog beside Kronos, following the existing desktop navigation.
2. Automatically backtest each valid generated version using a saved, visible preset. Require the user to configure a preset before the first submission; do not silently choose a date range or universe.
3. Never automatically make an AI version the default. The user explicitly selects **Set default** after validation and a successful backtest. A completed backtest with zero trades is valid but clearly labeled; profitability is not an automatic promotion criterion.
4. **Delete version** archives it from normal selection while preserving historical results and provenance. Deleting the default requires choosing a replacement in the same transaction. Keep at least one usable version. Physical deletion/garbage collection is outside the initial scope.
5. Initial scope edits existing chart patterns, including their paired documentation. Creating an entirely new pattern or changing shared engine code through AI is outside this release.
6. Use **DeepSeek** directly with **DeepSeek-V4.1-Flash**, API model ID `deepseek-flash`. Verified on 2026-09-18 in the [official release documentation](https://api-docs.deepseek.com/news/news260910/). Keep credentials and endpoint/model configuration server-side; do not silently substitute another model. Record the requested model and returned model metadata because an API alias may change over time. Validate credentials and API compatibility during implementation.

7. Use **PostgreSQL only** for durable pattern/editor storage: source blobs, versions, lifecycle state, jobs, presets, reports, and results. No SQLite backend, local registry, or test-only SQLite substitute.
8. Seed every initial pattern version directly from the actual repository pattern files and required dependencies. Preserve source bytes and behavior exactly; no AI rewriting, formatting, or logic changes during import. Initial defaults must be these verified file-derived baselines.

## User flow

1. Open Patterns; select a pattern and a base version. Show the default badge, version history, source, documentation, and prior backtest results.
2. Enter an instruction such as “Require two closes above the neckline before entry.” Show the selected backtest preset and allow changing it before submission.
3. Press **Submit**. Persist the request and base-version ID immediately; show generation progress and allow cancellation.
4. AI returns replacement pattern source, updated documentation, and a short explanation. Show a source diff against the selected base.
5. Persist an immutable version when a usable source response exists, then validate it. Failed versions remain visible with diagnostics; they cannot become defaults or execute in ordinary trading.
6. Automatically queue a stream backtest after validation passes. Compare it with the base version on exactly the same frozen dataset and settings, reusing a prior baseline result only when all inputs match.
7. Show progress, trades, equity, drawdown, win rate, net P&L, fees, and open positions at the end. Keep errors distinct from successful runs with zero trades.
8. Offer **Set default**, **Backtest again**, **Edit from this version**, and **Delete version**, with eligibility and conflict messages.

## Database and version lifecycle

Replace the SQLite-backed store with a PostgreSQL repository using ordered, versioned migrations in a dedicated `pattern_editor` schema. Reuse applicable store/service interfaces and immutability guarantees, not SQLite SQL or initialization logic. Store source bytes in `BYTEA`, structured manifests/reports in `JSONB`, and timestamps in `TIMESTAMPTZ`. PostgreSQL is the sole durable store for this feature; local files may only be import inputs, exports, or disposable sandbox materializations. Back up PostgreSQL before schema changes and preserve the original source files unchanged.

Use process-local connections/pools; never pass connections into spawned workers. Use PostgreSQL transactions, uniqueness constraints, row locks for version-number allocation/default changes, and a migration/import advisory lock. Claim durable jobs transactionally (for example, `FOR UPDATE SKIP LOCKED`) with ownership/lease recovery. An unavailable or unconfigured PostgreSQL connection produces an actionable error, never an alternate database or file fallback. Run storage integration tests against isolated PostgreSQL databases/schemas.

| Record | Required information |
| --- | --- |
| Pattern | Stable ID, display name, enabled state, default version ID, concurrency generation |
| Immutable version | ID, pattern ID, monotonic version number, parent ID, source/document references, dependency manifest, content hash, creation time, user instruction, provider/model, explanation, runtime compatibility manifest |
| Database content blob | SHA-256 key, exact original bytes (`BYTEA`), media type, size; stores executable source, documentation, and versioned pattern dependencies |
| Version lifecycle | Version ID, validation state, archive timestamp; separate from immutable source/version payload |
| Edit job | Request ID, idempotency key, base version, instruction, state, timestamps, error details, generated version ID |
| Backtest preset | Market, resolved symbols/universe policy, timeframe, dates or session count, warmup, execution settings, costs, gates, end-of-run policy |
| Backtest run | Exact version set, frozen preset, resolved symbols, input-data hash/reference, engine/runtime identity, status, progress, metrics, trades, logs |

Preserve stable pattern IDs from the detector classes and all new audit links. Legacy registry versions are not the source of initial baselines or defaults. Keep code immutable and lifecycle state separate so the current immutability guarantees remain meaningful. Use foreign keys and transactional checks to ensure a default belongs to its pattern and is not archived. Allocate version numbers transactionally without reusing deleted numbers. Default changes use optimistic concurrency to detect simultaneous edits.

### Exact file-to-PostgreSQL bootstrap

1. **Freeze the input.** Snapshot the actual working-tree bytes of `patterns/` and required repository dependencies at import time, including uncommitted changes. Record the Git commit when available, dirty state, relative paths, byte lengths, and SHA-256 hashes in an import manifest. A Git commit alone is insufficient to identify modified files. Detect any source change during collection and abort/retry instead of mixing revisions.
2. **Inventory all detectors.** Enumerate numbered detector modules and inspect their locally defined `BasePattern` subclasses using the trusted pre-cutover file-loading path, with database overrides explicitly disabled. Record each class’s actual `name`, module/class identity, timeframes, constants, `skipped` flag, and configured disabled state. Do not infer identity solely from filenames or silently omit skipped/disabled patterns. Helpers, tests, `chart_scan.py`, and `base_pattern.py` are not independent pattern records.
3. **Capture exact content.** Store each detector’s `.py` bytes and paired `.md` bytes unchanged, including encoding and line endings. Capture transitive pattern helper dependencies and record trusted engine/interface dependency hashes plus runtime/package versions. Audit static dependency collection for dynamic imports; explicitly resolve missing dependencies rather than silently loading mutable repository helpers later. Missing documentation/dependencies or ambiguous detector identity blocks that import and appears in the report.
4. **Create file-derived baselines.** In PostgreSQL, create version 1 with no parent, actor `file-import`, and the snapshot manifest. Preserve original pattern IDs and availability settings. Set each pattern’s initial default to its imported baseline; skipped/disabled patterns remain unavailable to ordinary discovery/execution. Do not use old registry active versions, cached imports, or AI output as substitutes. The existing validator rejects skipped patterns, so implement an import-specific inspection path that preserves their source and disabled status without enabling them.
5. **Verify byte equality.** Read every stored source/document/dependency blob back from PostgreSQL and compare its bytes, length, and SHA-256 against the frozen input. Produce an inventory report covering every numbered detector, its ID, source hash, dependency hashes, availability state, imported version ID, and verification outcome. No unexplained omissions or extras are allowed.
6. **Verify behavior before cutover.** Execute the frozen file baseline and its database-loaded equivalent in separate clean workers with the same runtime, candles, configuration, and initial dedup state. For every enabled detector, compare metadata and normalized signals, then backtest trades, entry/exit times and prices, quantities, fees, reasons, and P&L. Ignore only newly assigned provenance IDs; use explicitly justified numeric tolerances. Include triggering and non-triggering fixtures so a zero-signal run is not sufficient proof. For skipped/disabled detectors, verify byte/metadata equality and exclusion from ordinary execution; do not remove their skip flags for testing.
7. **Publish atomically.** Stage imports as non-executable until the complete inventory, byte verification, and behavior checks pass. Activate the verified import/default set transactionally. A failed import leaves no partially published catalog. Store the verification report in PostgreSQL; block cutover on any unexplained difference.
8. **Make reruns safe.** Reimporting the same snapshot creates no duplicate patterns/versions. A changed file after bootstrap produces an explicit conflict/report; it cannot overwrite immutable version 1 or change a selected default. Any later file import is a separate, deliberate new-version operation.
9. **Cut over all consumers.** Replace file enumeration and SQLite existence checks in loaders, worker acknowledgements, and scanner/backtest/UI consumers with PostgreSQL queries. Remove source-mirror activation/recovery dependencies. Defaults resolve once at run start and propagate as explicit version IDs. Empty PostgreSQL catalogs require the bootstrap command; no silent file fallback remains.

Current repository inventory (verify again at implementation time):

| Source module / paired `.md` | Pattern ID | Current `skipped` state |
| --- | --- | --- |
| `002_double_top` | `pattern_002_double_top` | false |
| `003_double_bottom` | `pattern_003_double_bottom` | false |
| `004_rounding_bottom` | `pattern_004_rounding_bottom` | false |
| `005_rounding_top` | `pattern_005_rounding_top` | false |
| `006_upward_channel` | `pattern_006_upward_channel` | false |
| `007_descending_channel` | `pattern_007_descending_channel` | true |
| `008_head_and_shoulders` | `pattern_008_head_and_shoulders` | true |
| `009_flag_pattern` | `pattern_009_flag_pattern` | true |
| `010_pennant` | `pattern_010_pennant` | false |
| `011_breakout_retest` | `pattern_011_breakout_retest` | true |

All ten source/document pairs are under `patterns/`; `DISABLED_PATTERNS` is currently empty, independently of class-level `skipped` flags. Import all ten; preserve the six currently available detectors and the four skipped detectors. Shared helpers such as `_channels.py`, `_rounding.py`, `_rules.py`, and `_dedup.py` must be captured when required. Trusted engine/interface code remains application code, with compatibility recorded and checked.

Existing SQLite files and `pattern_versions/` artifacts may be retained untouched as legacy backups, but the new application and bootstrap do not read them. Migrating historical editor records, if later requested, is a separate task and must never replace file-derived initial baselines/defaults. Rollback restores a verified PostgreSQL backup or the prior application deployment; it is not an automatic runtime storage fallback.

Changing the default affects new runs. Existing backtests and paper sessions retain their pinned versions until an explicit restart/reload boundary. Existing positions retain their original version and execution provenance. Archived versions remain readable by historical runs and already pinned sessions, but cannot be selected for new runs.

## Shared backtest service and historical replay

Create a UI-independent backtest request/schema and runner service, for example `core/backtest_service.py`. Web, desktop, and automatic editor jobs use this same contract. Retain offline mode for existing workflows and add an explicitly labeled **Historical stream** mode.

The stream request includes market, symbols/universe, timeframe, start date, end date or session count, warmup, initial capital, sizing, transaction costs, slippage, enabled gates, pattern-only mode, exact version IDs, and end-of-run handling. Validate mutually exclusive or unsupported options rather than ignoring them. Start with supported daily replay; expose other timeframes only when implemented.

Execution requirements:

- Reuse or extract the paper scanner/account orchestration and stream advancement protocol. Feed historical bars through the same ordering of position management, pattern analysis, pending entries, fills, and accounting used by paper trading.
- Give each job its own replay clock/session, account, deduplication state, workers, and output paths. Do not attach to or advance the user's running paper stream, reuse its ledger, or mutate shared global settings. Refactor fixed stream endpoints into per-run configuration where necessary.
- Advance one simulation session only after all symbols and pattern work for the current session finish. Handle holidays, unequal symbol histories, missing bars, duplicate deliveries, and end-of-data explicitly.
- Expose only data available at the current simulated timestamp, including weekly aggregates. Warmup bars build indicators but do not generate trades before the requested start.
- Freeze input history for the candidate/base comparison. Save data references and hashes, resolved symbols, engine identity, and every effective setting so reruns can be reproduced or declared incompatible.
- Default stream end behavior follows paper trading: retain open positions and report realized and unrealized results. Offer an explicit force-close option and label resulting exits. Keep offline end-of-data behavior visible when comparing modes.
- Support cancellation, durable status, progress, errors, and resource cleanup. A restart must mark interrupted runs clearly; retry creates a traceable attempt. Resume is optional and must not be claimed without persisted replay/account state.
- Run independent jobs without sharing a single global result slot. Both frontends query the same run/status/result service; desktop uses background execution and main-thread widget updates.

Audit `main.py` launch paths so both frontends initialize the required history providers. The standalone CLI currently rejects `--stream` without `--paper`; changing that CLI behavior is optional unless the shared service is exposed through `--backtest --stream` in this release. Frontend stream support is mandatory regardless.

## AI editing and validation

Implement a provider adapter and durable edit coordinator, reusing store/service interfaces through the new PostgreSQL implementation and the existing sandbox rather than executing model output in the web or UI process.

- Send the selected immutable source/documentation, pattern interface, allowed edit scope, and user instruction. Include only necessary dependencies; never credentials or unrelated files.
- Require a structured response with source, documentation, and explanation. Bound input/output size, request duration, retries, and concurrency; record model identity and provider errors.
- Use an idempotency key so duplicate Submit requests do not create duplicate versions. Pin the base version even if another user changes the default while generation runs.
- Validate syntax, allowed paths/imports, unchanged pattern identity, interface compatibility, finite signal fields, and relevant regression fixtures. Preserve the existing no-network, bounded-resource execution boundary. If the sandbox is unavailable, report a blocked job and do not fall back to unrestricted execution.
- Keep trusted harness/engine/shared helper edits outside the model's editable scope. Database-backed source remains executable code and needs the same isolation as current candidates.
- Avoid launching a fresh expensive sandbox for every bar if it makes replay impractical: use a bounded isolated worker for a run or batch while maintaining run isolation. Measure throughput before finalizing this design.
- Track `queued → generating → validating → backtesting → completed`, with explicit `failed`, `cancelled`, and `blocked` outcomes and separate validation/backtest reports. Generation failures without usable code retain the request/error but do not create a fictitious runnable version.
- A valid version survives a data/provider/backtest failure. Allow retrying its backtest without another model call or another version.

## Frontends and service surface

Proposed service/API operations:

- List patterns, versions, version details/source/diff, and presets.
- Submit an edit against a specific base version and preset.
- Read/cancel edit jobs and backtest runs; retrieve reports and trades.
- Launch a backtest against explicit version IDs.
- Set a default with the expected pattern generation.
- Archive a version, optionally replacing the default atomically.

Add a web `/patterns` route, template, and client script, using existing authentication and mutation protections. Extend the web backtest form with mode, replay settings, and version selection. Build a desktop Patterns dialog and extend `ui/backtest_dialog.py` with the same fields and validation. Move shared form/schema definitions out of the desktop module so headless web startup does not depend on Tkinter UI code.

## Implementation order

1. **PostgreSQL storage and exact bootstrap:** replace the legacy store, add schema migrations and database blobs, freeze/import all file patterns, verify bytes and behavior, and document backup/rollback. Publish database-authoritative discovery only after baseline parity passes.
2. **Version service:** explicit version loading, default/archive operations, run pinning, worker propagation, and removal of source-mirror activation dependencies.
3. **Stream backtests:** shared request/runner, isolated replay/account state, durable jobs, cancellation, and deterministic parity tests. Wire both existing backtest frontends before adding AI automation.
4. **AI coordinator:** verified provider/model configuration, structured generation, immutable version creation, validation, and automatic candidate/base backtests.
5. **Patterns screens:** navigation, instruction textbox and Submit, history/diffs, progress/results, default selection, and deletion.
6. **Integration and rollout:** migrate a copy of existing data, compare baseline behavior, exercise both launch modes, document configuration and recovery, then cut over database loading.

## Acceptance and verification

- After migration, disabling access to original detector files does not prevent database pattern discovery or execution; trusted application interfaces remain available. Every imported version matches its original source hash and baseline fixture behavior.
- Storage, worker, job, and frontend integration tests use PostgreSQL. Neither startup nor any editor operation creates or opens a SQLite file; PostgreSQL outages never trigger a file/SQLite fallback.
- The bootstrap inventory accounts for all ten current detectors and paired documents, preserving skipped/disabled states. Initial default versions match the actual frozen working-tree bytes, including dependencies, with stored verification reports and per-pattern behavioral parity. A failing check prevents publication.
- A second migration run creates no duplicates. Missing/corrupt blobs, runtime incompatibility, and unresolved source conflicts produce actionable errors without silently substituting code.
- Multiple versions coexist; exactly one usable default exists per enabled pattern. Concurrent default changes and version creation are safe. Deletion preserves historical results and obeys replacement rules.
- A default change during a running job does not change that job's version set, including spawned workers.
- From both `main.py --web` and `main.py --ui`, a user can run and cancel a historical stream backtest with a selected version and inspect persistent results.
- Pinned fixtures produce matching signal times, entries, exits, quantities, costs, and P&L through paper replay and the new stream backtest under identical settings. Test pending entries, deduplication, missing bars, warmup, gates, and end-of-run positions.
- A future-bar mutation cannot affect earlier signals/fills. Two simultaneous replay jobs cannot alter each other's clock, account, dedup state, or the active paper session.
- Submit with a mocked provider creates exactly one immutable version and automatically validates/backtests it. Invalid code, malformed responses, timeouts, cancellation, missing data, and unavailable sandbox/model produce visible and recoverable outcomes.
- An unsuccessful candidate never changes the default. Repeating only a failed backtest does not regenerate code.
- Complete browser and desktop smoke checks for navigation, submission, progress, results, default selection, deletion, and restart recovery. Run the existing pattern-loader/version/validation and backtest-paper parity suites plus targeted new integration tests.

## Confirmed scope and remaining configuration

The user confirmed both frontends, saved backtest presets, DeepSeek as the direct provider, PostgreSQL-only storage, and accurate initial seeding from the file patterns. The exact initial symbols, dates, market, and costs will be selected when saving the first preset; these do not block implementation planning. The model ID has been verified against official documentation as described above.
