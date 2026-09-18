# Exact baseline bootstrap (P03)

P03 adds an explicit, verified PostgreSQL import. Normal scanner/UI discovery still uses the existing consumer path until P04. Run this command only after applying editor migrations, with the intended `PATTERN_EDITOR_DATABASE_URL` configured:

```bash
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ \
  .venv/bin/python scripts/import_pattern_baselines.py
```

The three fixture settings above are for the disposable rehearsal. For a deployment use the application's actual settings and disabled-pattern configuration. The command prints the immutable import batch ID, snapshot hash, verification report ID, and status. It never chooses a legacy registry version, rewrites pattern files, enables skipped detectors, or creates a filesystem source registry. A deployment/cutover remains P11 work.

The import captures all ten numbered detector/document pairs, including skipped and configured-disabled patterns. Metadata comes from privately loaded actual classes. Each version stores the complete captured pattern dependency closure, including all detector targets in `_rationale._MODULES`; shared blobs deduplicate by SHA-256. This intentionally captures the other detectors used for dynamic rationale lookup. Missing repository dependencies, documentation, ambiguous classes, unrecognized dynamic imports, changed inputs, or incompatible trusted runtime code fail explicitly.

Trusted interfaces and engine code remain application code. Their transitive source fingerprints, Python identity, and installed distribution versions are compatibility inputs. Pattern modules and helpers load from captured bytes with private module and cache state; dynamic rationale imports also use those bytes. The engine supplies a copy of its current dedup context to each baseline evaluation. Generated candidates retain the existing isolated validator execution path; these trusted import workers do not establish production candidate-sandbox readiness.

Staging is one advisory-locked PostgreSQL transaction and creates no discoverable defaults. All blobs are read back and checked against frozen bytes, sizes, and hashes. Behavioral checks run outside publication transactions in fresh spawned processes, separately loading conventional Python files from a disposable snapshot and the explicit PostgreSQL version ID. The final transaction publishes all ten baseline defaults and lifecycle/report links together; only the six non-skipped, non-disabled patterns appear in `active_set()`.

Identical reruns reuse the immutable batch and version IDs. Published reruns still check every blob and current source/runtime identity, then reuse the successful report without changing defaults. Changes to source, dependencies, runtime, or configured availability cause an explicit conflict. Unrelated Git changes are recorded as audit context but do not change snapshot identity. A failed verification keeps staged versions unpublished and records its failed report; an identical retry can verify the same batch. A rolled-back staging transaction leaves no partial batch. A publication interruption rolls back the entire default set.

## Behavioral evidence

Each available detector has a triggering and flat non-triggering daily-bar case. Both sides run the existing `_core_backtest_symbol` engine with the same frozen inputs, USD 10,000 notional, 0.001 transaction cost fraction, fractional quantities, and America/New_York session timezone. A frozen empty earnings calendar prevents live EDGAR lookups; no detector gate is replaced. This tests existing offline semantics, including its historical full-series lookahead, and does not claim the future P06 streaming/no-lookahead gate.

| Detector | Trigger fixture |
| --- | --- |
| 002 double top | Existing pinned TXN barcache |
| 003 double bottom | Existing pinned ADBE barcache |
| 004 rounding bottom | Existing pinned ON barcache |
| 005 rounding top | New synthetic `rounding_top_control.csv`: dome, reversal, target exit |
| 006 upward channel | Existing pinned C barcache, frozen earnings calendar |
| 010 pennant | New synthetic `pennant_control.csv`: impulse, contracting coil, breakout, trailing exit |

The non-trigger case preserves dates and replaces every bar with open/close 100, high 101, low 99, volume 1,000,000. The four skipped detectors receive byte and metadata verification without evaluating or enabling them. Reports persist fixture blobs, settings, calendar, full normalized signals/trades, blocked/filtered results, and aggregate accounting. Numeric tolerance is **zero**; only generated `pattern_version_id`, `signal_id`, and `trade_id` values are excluded. Provenance decoration is applied equally to both sides. A positive case must produce both a signal and a trade; empty matching outputs cannot pass it.

## Reproducible tests

Provision a private PostgreSQL cluster as described in [P02 operations](pattern-editor-postgresql.md), and set `PATTERN_EDITOR_TEST_DATABASE_URL` explicitly to its disposable database. No application DSN is used by the integration fixture.

```bash
WATCHLIST=FIXTURE TV_SCREENER=america TV_EXCHANGE=NASDAQ \
  .venv/bin/python -m pytest -q \
  core/test_pattern_bootstrap.py core/test_pattern_edit_store.py \
  core/test_pattern_versions.py core/test_pattern_loader.py \
  core/test_pattern_edit_validation.py core/test_pattern_editor_contracts.py
```

Tests cover actual parity/publication, source preservation, private helper state, dynamic imports, detector-file removal, runtime incompatibility, changed inputs, missing documentation/dependencies, ambiguous identity, corruption, source mutation during collection, staging/publication rollback, concurrent staging/publication, and idempotent retry. The publication transaction test deliberately doubles parity; the full bootstrap test executes real workers for all ten detectors.

Verification results and disposable rehearsal IDs are recorded below after execution. No production catalog or default is changed by the recorded rehearsal.

## P03 verification record

- PostgreSQL **17.11**, private Unix socket `/tmp/pattern-editor-p03-pg/socket`, port 55483, database `pattern_editor_test`; TCP disabled. Schema migration 1; no additional migration needed.
- Final six-suite command above: **57 passed in 84.32s**, no skips. Two publication/conflict tests were then added and run with the same environment using `core/test_pattern_bootstrap.py -k 'concurrent_publication or changed_source_conflicts'`: **2 passed in 3.62s**, 10 deselected. Total: **59 passing tests**. [Output](verification/pattern-editor-p03-tests.txt).
- CLI migration applied once. Import and identical rerun returned the same batch `de4263e88f084c538ff78f74cbd7c214`, report `635d8d7adf264bbd8189ea91f31540a4`, and snapshot `ec429bff0fcb51b14fb7b49772c306b1d73c4276e447e4d8fc210ea11adfa1f5`. [CLI output](verification/pattern-editor-p03-import.txt), [verification report export](verification/pattern-editor-p03-report.json).
- SQL verification: 10 patterns, all published; 6 enabled; exactly 10 versions, all numbered 1; import state published. Each version references 44 exact captured files. PostgreSQL remains the authoritative report/blob store; the JSON export is evidence only.
- Random integration schemas were cleaned (zero remaining). `git diff --name-only -- patterns` returned no changes; compile checks and `git diff --check` passed. The private server was stopped after verification. Its disposable cluster retains the smoke-test catalog for inspection, not application use.
- Socket creation/access required an execution override limited to the private PostgreSQL test environment. No production database or application launch was used.
- Resume at **P04-01**. Normal file discovery, legacy existence guards, lifecycle eligibility services, and consumer version pinning are still P04 work.
