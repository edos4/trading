# Version lifecycle and consumer cutover (P04)

Normal discovery now requires a configured, migrated PostgreSQL editor catalog. Run the explicit bootstrap from [P03](pattern-editor-bootstrap.md) before starting the application. An empty/unpublished catalog reports the bootstrap command; connection, blob and runtime errors never trigger file or SQLite fallback. No production migration, import, or application rollout was performed during P04.

## Service operations

`core.pattern_versions.PatternVersions` provides:

- `catalog()`: published patterns, including availability, default IDs and generations.
- `list_versions(pattern_id, include_archived=False)`: immutable versions in numeric order with their lifecycle rows.
- `detail(version_id)`: immutable metadata/lineage, lifecycle, validation/import reports and persisted backtest/result summaries, including archived records.
- `source(version_id)`: exact source/document bytes; `diff(version_id, base_version_id=None)` compares against the immutable parent by default. Cross-pattern comparisons are rejected.
- `resolve(selected=None, disabled=())`: validate and pin the selected set, or all available defaults, once at run/session creation. Archived, skipped, disabled and unvalidated selections are rejected. Validation-passed candidates can be selected for backtesting before they have a successful backtest.
- `set_default(DefaultChange)` and `archive(ArchiveVersion)`: typed requests from `core.pattern_editor_contracts`, with expected pattern generation and idempotency key. The optional service `actor` is supplied by the trusted caller, not a client identity field.

Default selection requires passed validation evidence and a completed backtest with a stored result referencing that exact version. Zero-trade results qualify. Verified file imports use their exact-byte/parity report instead of a fictional generation/backtest job. A generated candidate never becomes the default merely because it is inserted or validated.

Archiving a default requires an eligible same-pattern replacement in the same transaction. The surviving default protects the last usable version. Versions, sources, parents, reports and results are never deleted or rewritten. Pattern row locks serialize creation/default/archive operations; generation comparisons reject stale mutations. Idempotency keys return the original committed response for an identical request and reject different requests. Default/archive audit events and worker acknowledgements are PostgreSQL records.

## Pinning and restart boundary

The scanner resolves at `MarketScanner.start()`. Each spawned analyze worker receives a copy of those exact IDs; pool creation and scan cycles do not query current defaults. Inline analysis and pool recreation use the same session set. Stop the paper scanner and start it again to adopt changed defaults. Existing positions keep their original version IDs and entry/execution rule snapshots.

`Backtester` resolves once in its constructor, including explicit `version_set` selections. Symbol workers accept only `version:<id>` specifications and check the pattern identity. They do not import a module/class fallback or resolve new defaults. An archived version remains loadable by an already pinned worker. New starts must go through `resolve()`; the explicit-ID loader is an internal execution/historical-read API, not a public new-run validation endpoint.

Web and desktop explorer discovery use the same PostgreSQL loader and retain their loaded set for the explorer instance's lifetime. Recreate the explorer/application to adopt new defaults. Their Patterns lifecycle controls remain P09/P10 work. Connections are opened per operation/process; only configuration and version IDs cross spawned-worker boundaries.

Signals carry immutable version IDs, signal IDs and requested rule snapshots. Existing trade, position, ledger, signal-log and export adapters preserve them. Annotation anchors also carry version IDs. Imported versions' chart descriptions use their captured helper/detector namespace. Generated code is never executed in the chart process; its saved annotations are displayed. Legacy charts retain saved anchors/geometry but no longer re-detect a setup or invent detector-rule explanations using current files.

## Consumer audit

| P01 consumer | P04 disposition |
| --- | --- |
| Loader discovery / legacy SQLite existence guards | Published PostgreSQL catalog only; guards and file fallback removed |
| Baseline snapshots / source-mirror recovery | Explicit import/test tooling only; never invoked by startup; legacy unfinished activation journals still fail without writing sources |
| Scanner / analyze pool | One version set per start; PostgreSQL acknowledgements; diagnostics identify versions |
| Backtester / UI pattern-name enumeration | PostgreSQL names and pinned worker specs; no runtime class enumeration |
| Web and desktop explorers | Existing adapters call database-only `discover()` |
| Paper account legacy cap lookup | Published PostgreSQL metadata; versioned signals use their saved cap |
| Chart reconstruction / dynamic rationale imports | Saved anchors, version-bound import helpers; no current detector re-import |
| Comparison utility | Shared database discovery and current Backtester arguments |
| Demo generation / golden numerical fixtures | Explicit developer/file tooling, separate from application discovery; skipped detectors stay testable without enabling them |
| Jobs, validator, evaluator | PostgreSQL blobs and existing sandbox boundary retained; no unrestricted generated-code fallback |

The actual numbered detector/document files and pattern helpers remain unchanged. The inventory remains ten patterns: six available and four skipped. Application runtime hashes change with this cutover; earlier P03 rehearsal imports belong to their old runtime. Use a fresh verified import for a fresh deployment, or explicitly reconcile an existing catalog/runtime under the later rollout procedure. Do not overwrite immutable versions or bypass compatibility checks.

## Verification

Tests use `PATTERN_EDITOR_TEST_DATABASE_URL` pointing only to the disposable private PostgreSQL server, with a generated schema per fixture and cleanup. Consumer/lifecycle fixtures explicitly double parity to isolate transaction tests; the full P03 bootstrap suite independently runs real spawned-worker file/database signal, trade and accounting parity for every available detector.

Two regressions surfaced by the combined gate were fixed. `collect_sources` gained a `prune_package` argument and `runtime_manifest` now prunes `patterns/`, so the trusted runtime fingerprint no longer depends on detector/helper sources and a source-less load verifies. `_core_backtest_symbol` resets the `patterns._dedup` walk registry before returning, so an in-process backtest cannot leak `_current`/`_used` into later paper, scanner or test work.

## P04 verification record

- Gate command and result: **179 passed**, 0 failed, 0 skipped, 14 warnings in 198.35s; output in [P04 tests](verification/pattern-editor-p04-tests.txt).
- Disposable PostgreSQL **17.11**, Unix socket `/tmp/pattern-editor-p03-pg/socket`, port 55483, database `pattern_editor_test` (reused private test cluster); schema migration 1; no additional migration required. Tests cover the empty-catalog/bootstrap error and connection outage without file fallback, version list/detail/source/diff and lineage, validation/backtest eligibility and idempotency, concurrent default/archive/create, archived-version rejection and pinned reads, one-level worker pinning (inline, spawned pool, backtester, scanner session boundary), PostgreSQL worker acknowledgements, and execution/provenance discovery without original detector sources.
- Randomized `pattern_editor_test_<uuid>` schemas were dropped by fixture teardown; `git diff --check`, compile checks and the original detector/document files were verified unchanged. The private test cluster was stopped after verification; production rollout: **not performed**.
- Conclusion: the P04 exit gate is met and P04 is complete. Resume at **P05-01**.

Sandbox execution of generated code remains a P06/P08 infrastructure gate; test validation/backtest records do not claim a live provider or production sandbox run.
