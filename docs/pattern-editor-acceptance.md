# Pattern Editor acceptance review (P11-07)

Every acceptance criterion in [pattern-editor.md](pattern-editor.md) is mapped to
passing evidence or an explicit, recorded limitation. Deployment status is
separate: **no production cutover was performed.**

| # | Acceptance criterion | Evidence | Status |
| --- | --- | --- | --- |
| 1 | After migration, losing access to the original detector files does not stop database discovery/execution; trusted interfaces stay available; every imported version matches its source hash and baseline parity. | P03 parity + byte/hash checks (E08, E09); P04 source-less discovery/execution tests (E11); P11-02 440/440 captured files byte-identical. | pass |
| 2 | Storage/worker/job/frontend tests use PostgreSQL; startup and editor operations never create/open SQLite; outages never fall back to file/SQLite. | P02 storage/outage tests (E05–E07); P09 503 mapping and no-file-fallback tests (E16); P11 rehearsal found no new SQLite file. | pass |
| 3 | Bootstrap inventory accounts for all ten detectors + paired docs, preserves skipped/disabled states, initial defaults are the frozen working-tree bytes incl. dependencies, with stored reports and parity; a failing check prevents publication. | E08/E09; P11-02: 10 patterns, 6 enabled / 4 skipped, 440/440 byte-identical, `published`, 6 defaults; failed-publication recovery proved state `failed` with 0 published. | pass |
| 4 | A second migration run creates no duplicates; missing/corrupt blobs, runtime incompatibility, and unresolved conflicts produce actionable errors without substituting code. | Migration rerun no-op (E06, P11-02); P03 conflict/corruption tests; P11-02 import rerun returned the same batch/report id; P02/P03 runtime-fingerprint errors. | pass |
| 5 | Multiple versions coexist; exactly one usable default per enabled pattern; concurrent default changes and version creation are safe; deletion preserves history and obeys replacement. | E11, E16 (default/archive with replacement + conflict); P11-04 default race produced exactly one `ok` and one `conflict` with 6 defaults set. | pass |
| 6 | A default change during a running job does not change that job's version set, including spawned workers. | E11 (pinning into inline and spawned workers); E15 (base pinned across a mid-flight default change). | pass |
| 7 | From both `main.py --web` and `main.py --ui`, a user can run and cancel a historical stream backtest with a selected version and inspect persistent results. | E14 (both launch modes, identical metrics); P09/P10 smokes; P11-01 suites. | pass |
| 8 | Pinned fixtures produce matching signal times, entries, exits, quantities, costs and P&L through paper replay and the stream backtest; pending entries, dedup, missing bars, warmup, gates, end-of-run positions covered. | `tests/test_backtest_paper_parity.py` + `core/test_stream_backtest.py` green in P11-01 (E13). | pass |
| 9 | A future-bar mutation cannot affect earlier signals/fills; two simultaneous replays cannot alter each other's clock/account/dedup or the active paper session. | E13 (future-bar mutation isolation; concurrent replay vs live paper account); P11-04 two concurrent runs completed independently with distinct notionals. | pass |
| 10 | Submit with a mocked provider creates exactly one immutable version and automatically validates/backtests it; invalid code, malformed responses, timeouts, cancellation, missing data, and unavailable sandbox/model produce visible, recoverable outcomes. | E15, E16 (each path asserted); P09/P10 smokes reached `blocked` with a preserved version. | pass |
| 11 | An unsuccessful candidate never changes the default; repeating only a failed backtest does not regenerate code. | E15/E16 (backtest-only retry with no provider call; no auto-promotion across default changes). | pass |
| 12 | Complete browser and desktop smoke checks for navigation, submission, progress, results, default selection, deletion, and restart recovery; existing suites plus targeted new integration tests. | E16/E17 smokes, [P11 browser smoke](verification/pattern-editor-p11-browser-smoke.txt), P11-01/P11-03. | pass, with limitation |

## Recorded limitations and remaining gates

- **Live DeepSeek call:** `DEEPSEEK_API_KEY` is not configured here. All provider
  verification is against doubles and the local stub; no real model call was
  made. Configure the key before declaring AI edits live.
- **Real candidate sandbox:** `PATTERN_EDIT_CGROUP_ROOT` / `PATTERN_EDIT_WORKER_PYTHON`
  are unconfigured, so generated candidates validate as `blocked`. The pipeline
  is exercised end-to-end with a sandbox double; the fail-closed path is
  asserted. Production requires a delegated cgroup v2 subtree.
- **Browser default/deletion clicks:** the browser smoke covers navigation,
  submission, progress, and reload reconnect; default selection and deletion are
  enforced and tested server-side (the buttons are only a fast path), so those
  specific browser clicks were not automated.
- **Detector causal-window limitation:** strict causal stream replay gates most
  ported detectors off by their own trailing-edge bounds; recorded in the stream
  handoff (P06). This is a detector property, not an editor defect.
- **Deployment:** not performed. The [cutover checklist](pattern-editor-cutover.md)
  is executable but has not been applied to a target environment.

## Tracker reconciliation

- Phases P01–P11 are marked complete in
  [the implementation plan](pattern-editor-implementation-plan.md); each phase
  checkpoint lists its changed files, evidence id, and next action.
- Evidence index rows E01–E18 point at recorded test/smoke output.
- Outstanding gates (live provider, real sandbox, production cutover) are
  recorded here and in the work log; they are prerequisites for the *feature*
  going live, not missing editor implementation.
