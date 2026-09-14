# Pattern editor implementation plan

Requirements: [AI-assisted pattern editing](pattern-edit.md). This file tracks implementation work; the requirements document defines the agreed behavior. Proposed module names below may change during implementation if the mapping is recorded here.

Deliver the complete editing workflow in both `python main.py --ui` and `python main.py --web`: select or drag chart points, describe a general rule change, generate and validate a preview, review the explanation and diff, Apply, inspect history, and roll back. Desktop operation must not require a browser or running web server.

## Current checkpoint

| Field | Current value |
| --- | --- |
| Overall status | In progress — P1–P8 code complete; P9 platform/provider verification open |
| Last updated | 2026-09-14 |
| Last completed task | P2.4 — version-aware time-exit policies and rule preservation |
| Current task | P9.1–P9.6 — integration verification and operational handoff |
| Next task | P9.4 — configured DeepSeek image-input smoke test; then P9.2 desktop run |
| Active branch / worktree | `main`; `/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22` |
| Latest implementation commit | None; all P0–P8 files are uncommitted in the working tree |
| Latest verified checkpoint | 128 tests passed (42 feature) + parity; real-data flow create→message→generate→validate→apply→rollback verified with a mocked runner; live HTTP and browser UI verified against a running `--web` server |
| Blockers | Q002 sandbox enforcement unconfigured → sandbox-backed preview/Apply unverified; Q003 live provider P9.4 unverified; Q006 pre-existing stale 009 discovery test; desktop display E2E P9.2 not run |
| Resume instruction | Read [operation guide](pattern-edit-operation.md) and [P0 contracts](pattern-edit-contracts.md), provision Q002/Q003, then run P9.2 desktop and the two-process P9.3 check |

## Tracking and resume protocol

Task checkboxes are the source of truth for completion. Stable IDs such as `P2.3` must survive edits to this plan. Phase states are `Not started`, `In progress`, `Blocked`, and `Complete`. An unchecked task may be in progress; record that in the checkpoint rather than marking it complete early.

At the start of each implementation session:

1. Read the requirements, checkpoint, phase table, blockers, and latest session-log entry.
2. Inspect `git status --short` and relevant diffs. Preserve existing work and distinguish this feature's changes from unrelated changes. Do not assume a recorded commit includes uncommitted work.
3. Select the next unchecked task whose dependencies are complete. If resuming partial work, inspect its recorded files and remaining steps first.
4. Confirm that prior verification still applies to the current code. Re-run checks only when intervening changes, failures, or missing evidence justify it.
5. Update the checkpoint's current task and phase state before substantial work.

After each meaningful task or before stopping:

1. Record changed files, the concrete result, and exact verification commands/results in the evidence table. Link local artifacts using repository-relative paths; include a commit reference if one exists. Do not require a commit just to resume.
2. Check a task only when its deliverable and required verification are complete. Mark the phase Complete only when all tasks and its exit gate pass.
3. Record failed checks, unavailable prerequisites, and partial implementation explicitly. A skipped required test does not count as a pass.
4. Update the checkpoint with the smallest actionable next step, remaining concerns, and any uncommitted work.
5. Append a session-log entry. Preserve earlier entries and decision records so later sessions understand why the implementation took its current form.

If blocked, identify the exact task, evidence, required resolution, and any independent work that can continue. Do not mark the whole feature complete while a required platform or provider check remains unverified. This tracking concerns development progress; persisted user edit sessions are a separate application feature implemented below.

## Phase overview

| Phase | Outcome | Depends on | State |
| --- | --- | --- | --- |
| P0 | Contracts, fixtures, and implementation decisions | — | Complete |
| P1 | Durable versions, drafts, and recovery storage | P0 | Complete |
| P2 | Version-aware execution and preserved trade rules | P1 | Complete |
| P3 | Shared editing service and isolated candidate execution | P1, P2 | In progress |
| P4 | DeepSeek multimodal generation and context | P3 | Complete |
| P5 | Executable preview, validation, and broader comparison | P2, P3, P4 | In progress |
| P6 | Complete native desktop editor controls | P5 | In progress |
| P7 | Complete web editor controls and HTTP adapters | P5 | In progress |
| P8 | Apply, activation acknowledgements, history, and rollback | P2, P5, P6, P7 | In progress |
| P9 | Integration verification and operational handoff | P8 | In progress |

Recommended sequence: P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8 → P9. P6 and P7 are independent once their shared service contracts are stable. Their Apply/history controls are wired to service contracts and tested with fakes until P8 completes real activation. Keep user-facing activation unavailable until its required checks pass.

## P0 — Establish contracts and reproducible fixtures

**Relevant files:** `patterns/base_pattern.py`, `patterns/008_head_and_shoulders.py`, its `.md`, `core/backtester.py`, `core/paper_trader.py`, `core/paper_books.py`, `core/pattern_jobs.py`, `core/scanner.py`, `core/signal_log_store.py`, `analysis/chart_renderer.py`, `ui/app.py`, `ui/tv_chart.py`, `web/app.py`, and `web/services.py`.

- [x] **P0.1** Trace signal creation, deferred entries, position creation, exit evaluation, JSON persistence, logs, exports, and every pattern-discovery cache. Record where version IDs and resolved rules must travel, including pending signals created before an activation.
- [x] **P0.2** Define typed contracts for candle identity, semantic anchors, trade provenance, dataset cutoff/hash, version manifests, edit sessions, candidate revisions, validation reports, and activation status. Preserve compatibility with old records and charts.
- [x] **P0.3** Define how edits express supported entry, exit, stop, and target changes through `TradeSignal` and the shared paper/backtest engine. Identify whether versioned executable exit hooks are needed beyond current fields. Generated edits must not silently reach into unversioned execution-engine code to implement a rule.
- [x] **P0.4** Select small deterministic OHLCV fixtures: a head-and-shoulders correction, an entry/stop/exit change, an unchanged control, and a case where no pattern should be detected. Record expected outcomes from inspected data, independent of AI-generated tests; include legacy trades and both paper markets in the test matrix.
- [x] **P0.5** Record decisions for transactional storage, dependency isolation, candidate sandbox, and Tk/browser image capture. Verify local display/sandbox capabilities and the current DeepSeek image API contract; use the configured provider smoke check later in P9 for credential-dependent verification.

**Exit gate:** Data contracts and fixture expectations are documented in the code or a linked design note, no unresolved design issue prevents preservation of existing position rules, and the chosen sandbox can enforce the specification. Unsupported host capabilities have an explicit setup path and fail-closed behavior.

P0 deliverables: [contract/design note](pattern-edit-contracts.md), [typed models](../core/pattern_edit_models.py), [fixture manifest](../tests/fixtures/pattern_edit/manifest.json), [trusted tests](../core/test_pattern_edit_contracts.py), and [host probe](../scripts/probe_pattern_edit_host.py). Exit gate passed at design/fixture scope: no runtime editing is enabled; unsupported host setup fails closed by contract, with production enforcement and adversarial tests required in P3.

## P1 — Implement persistent versions and edit sessions

**Proposed files:** `core/pattern_versions.py`, `core/pattern_edit_models.py`, `core/pattern_edit_store.py`, and `core/test_pattern_versions.py`. Proposed storage: immutable artifacts under `pattern_versions/` outside pattern discovery, with a SQLite registry in the application's local data directory. Confirm final paths in P0.

- [x] **P1.1** Create schema migrations and a transactional registry for patterns, versions, active references, edit sessions, immutable candidate revisions, jobs, and activation journal entries. Use stable IDs and cross-process transactions, not only thread locks.
- [x] **P1.2** Snapshot baseline Python source, paired Markdown, and required helper dependencies before the first edit. Hash content and record schema/runtime compatibility metadata; reject missing or corrupted artifacts.
- [x] **P1.3** Persist conversation, selections, image/data references, provider metadata, candidate descriptions, parent version, exact patch, and validation associations. Separate mutable session progress from immutable candidate content.
- [x] **P1.4** Implement staging and restart recovery for artifacts and the source mirror. Detect manual changes to active `.py`/`.md` files and preserve them for explicit reconciliation; never overwrite them silently.
- [x] **P1.5** Verify baseline creation, round-trip persistence, simultaneous writers, interrupted writes, missing artifacts, and reopening drafts after process restart. Protect artifacts referenced by versions, trades, or active drafts from cleanup.

**Exit gate:** Two processes can read and update the registry consistently, immutable snapshots survive restart, and no partial write becomes an executable active version.

## P2 — Carry versions through execution and trade management

**Relevant files:** `patterns/base_pattern.py`, `core/pattern_jobs.py`, `core/scanner.py`, `core/backtester.py`, `core/paper_trader.py`, `core/paper_books.py`, `core/signal_log_store.py`, `utils/trade_export.py`, `ui/app.py`, and `web/services.py`. Proposed loader: `core/pattern_loader.py`.

- [x] **P2.1** Load an explicit immutable version and its helper dependencies without contaminating other versions through `sys.modules`. Preserve shared base-class identity, skipped/disabled pattern rules, and reproducible imports; record runtime incompatibility rather than silently substituting current helper code.
- [x] **P2.2** Route all discovery paths and cached instances through the loader. Define a version set per scan/backtest job and pass it to spawned workers. Include paper-account caches such as `_MAX_OPEN_BY_PATTERN`; namespace deduplication state by version and define duplicate-entry handling across a switch.
- [x] **P2.3** Add `pattern_version_id` and a complete resolved rule snapshot to signals, pending entries, positions, closed trades, chart payloads, logs, and exports. Persist stable trade IDs so sorting or closing a row cannot select the wrong edit target.
- [x] **P2.4** Preserve entry-time rules through Apply, rollback, process restart, and future exit checks. Version-bound executable exit hooks remain contract-only (`ExitHookRef`/`exit_v1`): candidates are validated against the `TradeSignal` field allowlist and cannot introduce a hook, so no unversioned code path is reachable (fail closed). Sandboxed hook execution stays blocked on Q002. `time_exit_only_unfavorable`/`time_exit_min_mfe_pct` are now honored by the trusted engine.
- [x] **P2.5** Load old accounts and logs with explicit legacy/unknown provenance. Add regression coverage for old JSON, simultaneous old/new positions, cached workers, deferred entries, and paper/backtest exit parity.

**Exit gate:** Two versions can coexist; a position opened under the first closes under its original rules even when new signals use the second. Ordinary discovery remains compatible before any edits exist.

## P3 — Build shared orchestration and isolated evaluation

**Proposed files:** `core/pattern_edit_service.py`, `core/pattern_edit_jobs.py`, `core/pattern_edit_worker.py`, and focused service/worker tests.

- [x] **P3.1** Implement framework-independent operations for create/read session, append instruction/anchors, generate revision, request preview/comparison, cancel, and discard. Reserve typed Apply/history/rollback contracts for P8. Desktop calls the service directly; HTTP routes will be adapters.
- [x] **P3.2** Define persisted state transitions: draft → generating → validating → ready/needs-revision/failed, followed by applying → activation-pending → applied. Handle cancelled, discarded, and stale-base cases explicitly. New instructions create a new revision and invalidate prior preview/validation eligibility.
- [x] **P3.3** Run asynchronous jobs with durable IDs, progress, bounded retries, cancellation, and restart reconciliation. Bind every result to its revision so a late response cannot overwrite newer work. Prevent duplicate generation/Apply requests with idempotency keys.
- [x] **P3.4** Implement the P0 sandbox choice for candidate imports and execution: isolated writable scratch space, read-only required inputs, no network/broker access, sanitized environment, no provider secrets, and process/time/memory limits. Import-time Python executes inside the same boundary. Fail closed if required isolation is unavailable. Fail-closed behavior verified; enforcement inside a delegated cgroup remains unverified (Q002).
- [ ] **P3.5** Enforce file allowlists, resolved path/symlink checks, patch limits, and explicit handling of helper changes. Test escape attempts, runtime exceptions, timeouts, cancellation, malformed output, and recovery without changing active files or account data. Implementation present (allowlist, symlink/root checks, output/time limits, malformed-output rejection covered); actual escape/timeout/resource tests require the Q002 worker and are not run.

**Exit gate:** Both future interfaces can drive the same persisted service without FastAPI. Candidate failures cannot mutate active patterns, and sandbox limits have direct verification rather than relying on a subprocess label.

## P4 — Add DeepSeek generation with visual and code context

**Proposed files:** `analysis/pattern_edit_ai.py`, `core/pattern_edit_context.py`, provider/context tests, and configuration additions in `config.py` and the existing environment example if present.

- [x] **P4.1** Configure DeepSeek credentials, base URL, model ID, request limits, and timeouts. Use the specification's V4.1-Flash display name and verified API model ID; record requested and returned model identifiers. Keep credentials in the Python service and out of browser payloads and persisted prompts. (`config.py` and `.env.example`; live request deferred to P9.4.)
- [x] **P4.2** Assemble bounded, reproducible context: actual chart image, wider context image when needed, exact OHLCV/indicators, selected anchors, timeframe/timezone/cutoff, source/rules/dependencies, original trade, active baseline, candidate changes, conversation, and validation feedback.
- [x] **P4.3** Implement a structured response parser for candidate edits, description, explanation, anchor expectations, and unresolved questions. Prompt for general detection changes; explicitly describe requested changes to LOCKED rules and paired documentation. Treat repository text and chart content as context rather than permission to expand edit scope.
- [x] **P4.4** Support multi-message revisions and clarify conflicts inside the editor. Handle provider failures and rejected image input with actionable errors while preserving drafts; do not substitute a different provider or omit the image silently.
- [x] **P4.5** Test image-plus-text request construction, exact candle mapping, bounded context behavior, source-path validation, malformed responses, stale responses, and secret redaction with a mocked provider.

**Exit gate:** A fixture-backed multimodal request produces a parsed candidate in isolated draft storage with traceable context and no active-code modification. Real-provider verification remains explicitly tracked for P9.

## P5 — Produce real previews and validation reports

**Relevant files:** `patterns/chart_scan.py`, `analysis/chart_renderer.py`, `core/backtester.py`, `analysis/test_chart_viewer_payload.py`, and `analysis/test_pattern_overlays.py`. Proposed modules: `core/pattern_edit_validation.py` and `core/pattern_edit_comparison.py`.

- [x] **P5.1** Execute baseline and candidate against identical frozen data using the normal historical scan semantics. Render actual generated annotations and signal/rule outputs; distinguish historical trade annotations, current baseline, and candidate, including a no-detection result. (Orchestration verified with a fake runner; sandbox-backed execution pending Q002.)
- [x] **P5.2** Validate syntax/imports, interfaces, selected-anchor expectations, relevant existing tests, and code/rule/annotation agreement. Preserve trusted existing expectations; AI cannot make a candidate pass by deleting or weakening its validation suite.
- [x] **P5.3** Verify chronological cutoffs, future-data exclusion, and general behavior on control examples. Add scoped checks for symbol/date/index hardcoding and clearly report limits of automated semantic checks. Never infer correctness from a moved marker alone.
- [x] **P5.4** Bind reports to candidate, dataset, dependency, and validator/test hashes. Define required pass/fail/unavailable outcomes and readiness. Missing required data, failed tests, or unresolved required expectations must disable Apply.
- [x] **P5.5** Implement optional broader comparison with chosen symbols, dates, and timeframes on identical baseline/candidate datasets. Report added/removed signals, anchor and rule changes, and available trade metrics; persist results and expose progress/cancellation. Display Not run until requested.
- [ ] **P5.6** Verify the P0 correction and rule-change fixtures, no-pattern output, revised-candidate invalidation, deterministic results, comparison cancellation, and unavailable-data handling. Add relevant paper/backtest parity checks for changed exit behavior. Fixture-backed candidate execution and comparison cancellation require the Q002 worker and are not run.

**Exit gate:** A reviewer can see what the candidate actually does and why it is eligible for Apply. Neither the provider's explanation nor modified chart decorations substitute for executing and checking the candidate.

## P6 — Implement the native `--ui` editor

**Relevant files:** `ui/app.py`, `ui/paper_dashboard.py`, `ui/tv_chart.py`, and `ui/kronos_dialog.py`. Proposed additions: `ui/pattern_edit_panel.py` and focused desktop tests.

- [x] **P6.1** Extend open/closed trade chart creation with stable trade, pattern, version, and service context. Add optional editing callbacks so other `TradingViewChart` consumers continue working.
- [x] **P6.2** Implement real-candle context menus, semantic point handles, drag/snap behavior, candle-based alternative selection, and selected OHLC details. Resolve `_start + _index_at(...)` correctly, check price-plot bounds, exclude predicted candles, and preserve pan/zoom outside point dragging.
- [x] **P6.3** Add the native chat panel, visible How to edit instructions, multi-message revision flow, selection history, and generation progress. Capture/export the chart viewport with overlays and highlights; visually compare the image sent to AI with the Canvas display.
- [x] **P6.4** Add baseline/candidate preview, explanation, scrollable code/document diff, test details, broader-comparison controls, and Apply/Discard/history/rollback controls. Clearly identify legacy/older trades and require pattern selection when ambiguous. Wire final activation through P8's service contracts.
- [x] **P6.5** Run service jobs outside Tk's main thread and marshal updates through a queue polled with `after`. Handle slow jobs, cancellation, destroyed windows, duplicate clicks, reopened sessions, and process restart without freezing the app.
- [ ] **P6.6** Exercise native interaction with a mocked provider in a display-enabled test environment. Verify both open and closed rows, image correspondence after zoom/pan, point dragging, review flow, failures, and responsiveness with no web server or web password configured. Requires a display-enabled run (P9.2); not run. Unit mapping test: `ui/test_pattern_edit_panel.py`.

**Exit gate:** The desktop performs the full draft/review workflow inside Tk, with tested service controls ready for P8 activation. Browser tests do not satisfy this gate.

## P7 — Implement web adapters and chart editing

**Relevant files:** `web/app.py`, `web/templates/paper.html`, `web/static/app.js`, `web/static/tv_chart.js`, `web/test_paper_api.py`, and `web/test_e2e_playwright.py`. Proposed dedicated editor assets/tests may be split out for maintainability.

- [x] **P7.1** Add authenticated routes from the requirements, adapting the shared service. Validate trade identity, candle context, and file scope in Python. Include job progress/cancellation and enforce existing session and request-origin protections for mutations.
- [x] **P7.2** Add right-click candle selection, semantic anchor handles, drag/snap controls, and equivalent selection details in Lightweight Charts. Preserve scroll/zoom and exclude forecast candles from historical edits.
- [x] **P7.3** Add chat, instructions, context image capture, revised previews, explanation/diff/test panels, optional comparison, and Apply/Discard/history controls with the same service eligibility rules as desktop. Rollback control added.
- [x] **P7.4** Support reopening sessions, failed requests, cancellation, stale drafts, duplicate clicks, and visible activation state. Keep provider credentials and source-write authority out of JavaScript.
- [ ] **P7.5** Verify authenticated API behavior and actual browser interactions for open/closed trade rows using mocked generation. Check screenshot context, point mapping after zoom/pan, revision invalidation, and failure recovery. Adapter/auth/error-mapping verified by `web/test_pattern_edit_api.py`; live browser interaction requires P9.3 and is not run.

**Exit gate:** Web and desktop expose the same editing capabilities and service state, with no second implementation of validation or version storage in the web layer.

## P8 — Enable activation, history, and rollback

**Relevant files:** Registry, service, loader, scanner/worker lifecycle, both editor interfaces, and their integration tests.

- [x] **P8.1** Implement Apply for the exact displayed candidate and successful validation report. Compare the active base and source-mirror hashes, reject stale drafts, serialize per-pattern activation across processes, and make retries idempotent.
- [x] **P8.2** Stage immutable content, journal source-mirror updates, and atomically switch the active reference. Recover from crashes at each boundary without mixed executable versions. For shared-helper changes, isolate dependencies per version or activate an explicitly reviewed affected-pattern set atomically; document and test the chosen approach.
- [x] **P8.3** Switch scanner and worker versions at the defined boundary, retain in-flight job versions, and publish acknowledgements. Distinguish registered-active from worker-pending states, exclude stopped workers from indefinite waits, and refresh both explorers when versions change.
- [x] **P8.4** Implement history with descriptions, actor/time, explanation, instructions, test results, and arbitrary-version diffs. Allow description edits before Apply; later corrections are audited metadata events rather than silent rewriting of immutable source history.
- [x] **P8.5** Implement rollback as a new draft restoring a selected version and dependencies, followed by compatibility/selected-chart validation and explicit Apply. Record restoration provenance and reason without deleting history or changing existing position rules.
- [ ] **P8.6** Wire real Apply/history/rollback into both interfaces and verify concurrent desktop/web edits, retry after lost response, restart recovery, pending worker activation, manual file changes, and old-position/new-signal coexistence. Controls wired in both interfaces; concurrent two-process verification requires P9.3 and is not run.

**Exit gate:** A version applied from either interface becomes active predictably, rollback follows the same review path, and positions plus pending signals retain their original provenance and rules across restarts.

## P9 — Verify the complete feature and document operation

**Deliverables:** Recorded test evidence, desktop/browser artifacts, current usage/setup instructions, and a completed requirement mapping below.

- [ ] **P9.1** Run focused suites for the new service, registry, loader, sandbox, provider adapter, preview, and both interfaces. Run existing overlay, paper account, signal log, export, worker, and relevant slow parity/golden checks affected by the final diff. Record unavailable fixture dependencies separately. Focused and existing suites run and green (see evidence); sandbox-backed preview and live provider rows remain Not run.
- [ ] **P9.2** Complete a desktop end-to-end run from `python main.py --ui` → Paper Trading for open and closed rows: instructions, chat, drag, generation, visual context, preview, explanation/diff, validation, optional comparison, Apply, history, and rollback. Use a virtual display where appropriate and keep the web server off. Not run — requires a display-enabled session with Q002/Q003 provisioned.
- [ ] **P9.3** Complete the equivalent browser end-to-end run and a two-process shared-workspace check: a desktop activation is visible on web, a web activation is visible on desktop, and conflicting drafts cannot overwrite each other. Browser UI run done (`web/test_pattern_edit_e2e.py`: panel mounts, instruction sends, preview fails closed, Apply disabled) and live HTTP flow verified; desktop visibility and the two-process conflict check are not run.
- [ ] **P9.4** Run a separately configured DeepSeek image-input smoke test on a controlled fixture. Record model identity, image delivery/interpretation evidence, and response parsing without exposing credentials. No application Apply is needed for this provider check. If credentials or network are unavailable, record this task as unverified and keep the final gate open. Not run — no credential-dependent request made (Q003).
- [x] **P9.5** Document configuration, sandbox/display prerequisites, launching either interface, session recovery, version storage/backup, pending activation, manual-source reconciliation, and rollback. Verify that the visible How to edit guidance matches the shipped controls. See [operation guide](pattern-edit-operation.md).
- [ ] **P9.6** Review every source acceptance criterion against evidence, resolve remaining required failures, update this tracker, and record the final implementation checkpoint and remaining non-blocking limitations. Tracker updated and evidence recorded; final review remains open until P9.2–P9.4 complete.

**Exit gate:** All acceptance criteria below have passing evidence, both launch modes are verified, and no required task is represented as complete merely because it was skipped.

## Verification commands and scope

Use the project's configured Python environment. Existing test targets that are relevant to the feature include:

```bash
python -m pytest analysis/test_chart_viewer_payload.py analysis/test_pattern_overlays.py
python -m pytest core/test_pattern_jobs.py core/test_paper_books.py core/test_paper_trader.py core/test_signal_log.py
python -m pytest utils/test_trade_export.py utils/test_trade_display.py web/test_paper_api.py web/test_services_patterns.py
python -m pytest tests/test_backtest_paper_parity.py
python -m pytest core/test_pattern_edit_contracts.py core/test_pattern_versions.py core/test_pattern_edit_service.py core/test_pattern_loader.py core/test_pattern_edit_validation.py core/test_pattern_edit_integration.py
python -m pytest ui/test_pattern_edit_panel.py web/test_pattern_edit_api.py
python -m pytest web/test_pattern_edit_e2e.py  # standalone; needs a running --web server + real data
```

These commands are a starting point, not recorded passes. The parity suite is marked slow and depends on fixture data; select relevant cases during development and run the required final scope in P9. Add exact commands for new test files as they are created. The existing browser check is a standalone script; inspect its current options before adapting it. Native GUI tests require a real or virtual display, and sandbox integration tests must use the actual selected isolation mechanism. Keep mocked-provider suites separate from the configured-provider smoke test.

## Requirements coverage

Numbers refer to acceptance criteria in [pattern-edit.md](pattern-edit.md#acceptance-criteria).

| Criterion | Implementation tasks | Final evidence |
| --- | --- | --- |
| 1 — Open/closed row entry flow | P6.1–P6.2, P7.2 | Implemented both interfaces; candle mapping unit test passes; display/browser E2E P9.2/P9.3 not run |
| 2 — Instructions, chat, candle selection, dragging | P6.2–P6.4, P7.2–P7.3 | Implemented native + web; no automated display test (P6.6/P9.2) |
| 3 — Visual/data/code context and general edits | P4.2–P4.5, P5.3, P6.3, P7.3 | Mocked multimodal request test passes; live provider P9.4 not run |
| 4 — Executed preview, explanation, diff, validation | P5.1–P5.4, P6.4, P7.3 | Orchestration + report binding tested with fake runner; sandbox execution pending Q002 |
| 5 — Detection/trade rules and broader comparison | P0.3, P2.4, P5.5–P5.6 | Timer policies implemented and tested; parity 5 passed; broader comparison not directly tested |
| 6 — Draft isolation and exact activation | P3.2–P3.5, P8.1–P8.3 | Apply eligibility/hash binding implemented; sandbox escape tests pending Q002 |
| 7 — Persistent descriptions/history/rollback | P1.2–P1.5, P8.4–P8.5 | Registry round-trip/reopen tests pass; rollback-draft test passes |
| 8 — Future signals and preserved trade rules | P2.2–P2.5, P8.6 | Legacy provenance + paper/backtest parity tests pass; two-process E2E not run |
| 9 — Failure, conflict, recovery, and provider checks | P3.5, P4.5, P5.6, P8.6, P9.1, P9.4 | Malformed/sandbox-unavailable/stale-draft paths tested; live provider P9.4 unverified |
| 10 — Complete standalone desktop workflow | P6.6, P9.2 | Controls implemented; display E2E not run |
| 11 — Tk responsiveness and session recovery | P6.5–P6.6, P9.2 | Off-thread queue marshalling implemented; display test not run |
| 12 — Cross-interface version visibility/conflicts | P8.6, P9.3 | Shared registry + compare-and-swap implemented; two-process E2E not run |

## Decision log

Append implementation decisions with their rationale and affected task IDs. A plan detail can be refined without changing the agreed feature scope; update the requirements too if an explicitly agreed scope change occurs.

| ID | Date | Decision | Basis / affected work |
| --- | --- | --- | --- |
| D001 | 2026-09-14 | Both native desktop and web are required; desktop has no HTTP-server dependency | User requirement; P3, P6, P7 |
| D002 | 2026-09-14 | Share generation, validation, persistence, and activation services across interfaces | Requirements; P1–P8 |
| D003 | 2026-09-14 | General detection and trade-rule edits require preview and explicit Apply | User requirement; P4, P5, P8 |
| D004 | 2026-09-14 | Preserve immutable versions and entry-time position rules; rollback creates a new version | User requirement; P1, P2, P8 |
| D005 | 2026-09-14 | Use mocked-provider development checks plus a separately tracked real multimodal check | Requirements; P4, P9 |
| D006 | 2026-09-14 | SQLite at `data/pattern_edit/registry.sqlite3`; immutable content under workspace `pattern_versions/`; journaled source mirrors | P0.1/P0.5; P1/P8 |
| D007 | 2026-09-14 | Separate requested/fill rules; retain trusted engine hash and sandboxed `exit_v1` hooks for general exits | P0.2/P0.3; P2/P3 |
| D008 | 2026-09-14 | Private version dependency packages, frozen external inputs, version-scoped dedup plus cross-version duplicate-event guard | P0.1/P0.5; P2 |
| D009 | 2026-09-14 | Bubblewrap + seccomp + delegated cgroup/rlimits; fail closed without resource/isolation prerequisites | Host probe; P0.5/P3 |
| D010 | 2026-09-14 | Native Canvas PostScript export and browser chart/layer composite; verify chart correspondence in platform phases | P0.5; P6/P7 |
| D011 | 2026-09-14 | Causal-prefix selected-chart validation is required; legacy full-history backtests are separately labeled comparisons | Inspected correction fixture; P0.4/P5.3 |
| D012 | 2026-09-14 | Executable `exit_v1` hooks stay contract-only and unreachable from a candidate; edits are limited to the declarative `TradeSignal` field allowlist, so unsupported exit logic fails closed instead of running unversioned engine code. `time_exit_only_unfavorable`/`time_exit_min_mfe_pct` are honored by the trusted engine. | P0.3/P2.4; Q002 |
| D013 | 2026-09-14 | Frozen-dataset validation enforces only finite values, ordered unique timezone-aware timestamps, `high>=low` and non-negative volume. It does **not** require `low <= open/close <= high`, because real feeds contain split-rounded and stale sub-penny bars that violate intra-bar ordering. | Live verification; Q007; P3/P5 |

## Blockers and open implementation questions

P0 design choices are resolved in [pattern-edit-contracts.md](pattern-edit-contracts.md). Remaining setup and later-phase verification are explicit below.

| ID | Task | Issue / evidence | Resolution needed | State |
| --- | --- | --- | --- | --- |
| Q001 | P0.3 | Field behavior audited, including fill rebasing and previously ineffective unfavorable/MFE timer fields | Resolved: engine now honors `time_exit_only_unfavorable`/`time_exit_min_mfe_pct`; `exit_v1` stays contract-only and unreachable (D012) | Closed in P2; tested |
| Q002 | P0.5, P3.4 | Host Bubblewrap and Canvas export passed; cgroup v2 root is not writable; workspace restrictions block host probes | Design resolved; provision delegated cgroup worker service and verify actual resource/escape tests before enabling candidates | Open; P3 candidate isolation unverified |
| Q003 | P0.5, P9.4 | Official release/vision contract verified 2026-09-14; no credential-dependent request made | Complete configured image interpretation smoke check in P9.4 | P0 complete; P9.4 unverified |
| Q004 | P5, P9.1 | `chart_renderer._rsi_sma` returned empty RSI for monotonically rising input | Resolved by masking zero-loss/zero-gain rows (100 / 50); `analysis/test_chart_viewer_payload.py` passes | Closed |
| Q005 | P5.3 | Legacy backtest sees full history; correction fixture yields retrospective entry 204 while causal baseline emits no signal | Mandatory causal-prefix validation; legacy metrics separately labeled, never eligibility evidence | Design resolved in P0; implemented P5 |
| Q006 | P9.1 | `web/test_services_patterns.py::test_default_run_covers_ported_patterns_but_not_retired_011` expects `pattern_009_flag_pattern` by default, but `FlagPattern.skipped = True` at HEAD | Update the stale test expectation or re-enable 009; pre-existing and independent of the editor (same result with the old discovery code) | Open; unrelated |
| Q007 | P3, P5 | `validate_candles` required `low <= open/close <= high`, rejecting real charts (e.g. RPC 2026-08-25 open>high; CSDX sub-penny close<low), so `create` failed on genuine trades | Relaxed to the documented invariants (D013); all 15 sampled real charts now validate | Closed |

## Task evidence

Add one row per completed task or meaningful partial checkpoint. Use explicit Passed, Failed, Not run, or Partially verified results, including test counts where available. Never paste secrets or full provider requests containing sensitive data.

| Task ID | Date | Files / artifact / optional commit | Verification command or manual procedure | Result and remaining work |
| --- | --- | --- | --- | --- |
| — | 2026-09-14 | This plan | Reviewed requirements and current integration points | Documentation only; implementation not started |
| P0.1 | 2026-09-14 | [Runtime trace](pattern-edit-contracts.md#runtime-and-persistence-trace-p01) | Inspected all production pattern import/discovery paths, queues, fill/exit code, JSON/log/chart/export mappings | Passed; actual ordinary fills are signal-close despite stale scanner header; Kronos sleeve has next-bar pending fills |
| P0.2 | 2026-09-14 | [Typed models](../core/pattern_edit_models.py), [identity rules](pattern-edit-contracts.md#identity-artifacts-and-compatibility-p02) | `.venv/bin/python -m py_compile core/pattern_edit_models.py scripts/probe_pattern_edit_host.py core/test_pattern_edit_contracts.py` | Passed; additive contracts only, runtime validators/wiring remain P1–P3 |
| P0.3 | 2026-09-14 | [Rule boundary](pattern-edit-contracts.md#rule-boundary-and-preservation-p03), `ResolvedRules` | Inspected `_open_trade`, `_check_exit`, paper `_manage_position`; rule arithmetic/persistence tests in combined command below | Passed; engine/hook preservation design resolved; two currently ineffective timer fields explicitly flagged |
| P0.4 | 2026-09-14 | [Fixtures](../tests/fixtures/pattern_edit/manifest.json), [tests](../core/test_pattern_edit_contracts.py) | `.venv/bin/python -m pytest core/test_pattern_edit_contracts.py analysis/test_pattern_overlays.py -q` | Passed: 23 tests (13 P0, 10 overlays); initial 2 fixture failures corrected by explicitly selecting close-trigger stop for candidate timer scenario |
| P0.5 | 2026-09-14 | [Host probe](../scripts/probe_pattern_edit_host.py), [choices and sources](pattern-edit-contracts.md#storage-isolation-and-images-p05) | `.venv/bin/python scripts/probe_pattern_edit_host.py` outside workspace execution restrictions; official DeepSeek release/vision docs inspected | Passed basic host namespace/env/mount and Canvas export probes; cgroup delegation not configured, full P3 isolation tests and P9.4 provider smoke Not run |
| P0 regression audit | 2026-09-14 | Existing `analysis/test_chart_viewer_payload.py` | `.venv/bin/python -m pytest core/test_pattern_edit_contracts.py analysis/test_chart_viewer_payload.py analysis/test_pattern_overlays.py -q` then standalone `.venv/bin/python -m pytest analysis/test_chart_viewer_payload.py::test_viewer_payload_has_candles_and_levels -q` | Historical P0 run: 24 passed, 1 failed. Later fixed in P5 (zero-loss/zero-gain RSI masking) and now passes — Q004 closed |
| P1.1–P1.5 | 2026-09-14 | [store](../core/pattern_edit_store.py), [versions](../core/pattern_versions.py), [tests](../core/test_pattern_versions.py) | `.venv/bin/python -m pytest core/test_pattern_versions.py -q` | Passed: 5 tests — two-process baseline, corruption rejection, manual-edit conflict, session reopen/CAS, missing/symlink/escape artifacts |
| P2.1–P2.3, P2.5 | 2026-09-14 | [loader](../core/pattern_loader.py), [provenance](../core/pattern_provenance.py), `base_pattern`/`backtester`/`paper_trader`/`paper_books`/`scanner`/`pattern_jobs`/`trade_export` | `.venv/bin/python -m pytest core/test_pattern_loader.py core/test_pattern_edit_contracts.py core/test_pattern_jobs.py core/test_paper_books.py core/test_paper_trader.py utils/test_trade_export.py tests/test_backtest_paper_parity.py -q` | Passed: loader isolation, legacy provenance, deferred entries, paper/backtest parity (5) |
| P2.4 | 2026-09-14 | `core/backtester.py` `_time_exit_allowed` | `.venv/bin/python -m pytest core/test_pattern_edit_contracts.py -q` | Passed: 15 tests incl. favorable-timer suppression and MFE give-up floor; `exit_v1` unreachable (D012) |
| P3/P4/P5 orchestration | 2026-09-14 | [service](../core/pattern_edit_service.py), [ai](../analysis/pattern_edit_ai.py), [validation](../core/pattern_edit_validation.py), [service tests](../core/test_pattern_edit_service.py), [validation tests](../core/test_pattern_edit_validation.py) | `.venv/bin/python -m pytest core/test_pattern_edit_service.py core/test_pattern_edit_validation.py -q` | Passed: 10 tests — mocked multimodal draft/reopen, idempotent submit, scope rejection, fail-closed sandbox, report hash/readiness, anchor mismatch, malformed output, rollback draft |
| P6/P7 controls | 2026-09-14 | [panel](../ui/pattern_edit_panel.py), [tv_chart](../ui/tv_chart.py), [api](../web/pattern_edit_api.py), [client](../web/static/pattern_editor.js), [panel test](../ui/test_pattern_edit_panel.py), [api test](../web/test_pattern_edit_api.py) | `.venv/bin/python -m pytest ui/test_pattern_edit_panel.py web/test_pattern_edit_api.py -q` and `node --check web/static/pattern_editor.js web/static/tv_chart.js` | Passed: 7 tests (candle mapping, auth, origin guard, error mapping, history/rollback/apply); JS syntax OK |
| P8.1–P8.5 | 2026-09-14 | Apply/activation journal + history/rollback in [service](../core/pattern_edit_service.py), rollback route in [api](../web/pattern_edit_api.py) | `.venv/bin/python -m pytest core/test_pattern_edit_service.py web/test_pattern_edit_api.py -q` | Passed: rollback creates a reviewable draft; apply eligibility/idempotency/hash checks exercised via mocked provider |
| P9.1 | 2026-09-14 | Existing suites affected by the diff | Plan verification commands (see above) | 81 passed, 1 pre-existing failure (Q006); parity 5 passed; golden/RSI/scanner/execution-accounting 61 passed |
| P9.5 | 2026-09-14 | [operation guide](pattern-edit-operation.md), [.env.example](../.env.example) | Manual review against shipped controls | Written; deepseek/pattern-edit settings added to the environment example |
| Real-data live verification | 2026-09-14 | [integration tests](../core/test_pattern_edit_integration.py) | `.venv/bin/python -m pytest core/test_pattern_edit_integration.py -q`; plus a scratch run against a real 1227-candle paper chart | Passed: 3 tests — full create→message→generate→validate(ready)→apply(active)→history→rollback; idempotent apply; sandbox-down blocks apply and leaves the active source untouched. Real chart run reached `ready` with all checks passed and applied v2 |
| Q007 dataset validation | 2026-09-14 | [validate_candles](../core/pattern_edit_validation.py), [tests](../core/test_pattern_edit_validation.py) | Re-validated 15 real charts from `paper_books`; `.venv/bin/python -m pytest core/test_pattern_edit_validation.py -q` | Passed: previously 0/15 valid (strict intra-bar ordering), now 15/15 valid; vendor-quirk regression test added |
| P9 live HTTP | 2026-09-14 | [api](../web/pattern_edit_api.py), running `main.py --web` | urllib flow against `http://127.0.0.1:8080`: login → chart → create → message → preview | Passed: real closed trade chart exposes `edit_context` (1227 candles); create 200; message 200 (`draft`); preview job fails closed with "Configure DEEPSEEK_API_KEY", draft preserved, history baseline present |
| P9.3 browser half | 2026-09-14 | [e2e script](../web/test_pattern_edit_e2e.py) (screenshot gitignored) | `.venv/bin/python main.py --web` then `.venv/bin/python web/test_pattern_edit_e2e.py --base-url http://127.0.0.1:8080` | Passed: real browser mounts the editor on a real closed trade, sends an instruction, preview fails closed, Apply disabled. No JS page errors |

## Session log

### 2026-09-14 — Plan created

- Completed: Inspected the specification, desktop/web chart paths, pattern worker discovery, and paper/backtest persistence integration points; created phases and the resumable tracker.
- Implementation changes: None.
- Tests: No application tests run for this documentation task.
- Next action: P0.1 — trace and record runtime/persistence contracts before changing code.

Copy this template for later entries:

```markdown
### YYYY-MM-DD — Session label

- Tasks worked: Pn.n
- Completed and evidence: task IDs, files/artifacts, checks and results
- In progress: exact partial state and uncommitted files
- Decisions/blockers: IDs with rationale or required resolution
- Next action: one concrete step with file/function and required check
- Branch/worktree/commit: actual location and optional commit reference
```

### 2026-09-14 — P0 implemented

- Tasks worked: P0.1–P0.5, all complete at contract/fixture scope.
- Completed: runtime/persistence/discovery audit, typed wire contracts, general rule/hook preservation decision, four hashed deterministic datasets, 13 trusted P0 cases, native/sandbox host probes and official provider contract verification. See task evidence above.
- Tests: 23 P0/overlay tests passed. Existing chart-payload RSI failure reproduced independently and tracked Q004. Basic host probes passed outside workspace restrictions. Full resource enforcement, UI correspondence and live provider tests belong to P3/P6/P7/P9 and were not claimed as passes.
- In progress: none within P0; all new files and tracker edits are uncommitted. Original requirements and implementation plan were already untracked and preserved.
- Decisions/blockers: D006–D011. Delegated cgroup worker setup is required before P3 candidate execution. Full-history lookahead is explicitly isolated from mandatory causal validation. No P1 blocker.
- Next action: P1.1 — implement SQLite schema/migrations and cross-process registry at `data/pattern_edit/registry.sqlite3`, using `core/pattern_edit_models.py` and the contract note.
- Branch/worktree/commit: `main`, `/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22`; no commit created.

### 2026-09-14 — P1–P8 implementation and verification pass

- Tasks worked: P1.1–P1.5, P2.1–P2.5, P3.1–P3.4, P4.1–P4.5, P5.1–P5.5, P6.1–P6.5, P7.1–P7.4, P8.1–P8.5 complete; P9.5 complete; P3.5/P5.6/P6.6/P7.5/P8.6 and P9.2–P9.4 remain open on external prerequisites.
- Completed: durable SQLite registry and immutable artifacts with recovery; version-aware loader and provenance/rule snapshots threaded through scanner, workers, backtester, paper books, logs and exports; framework-independent editing service with persisted state machine and sandboxed candidate runner; DeepSeek multimodal adapter and strict response parser; validation orchestration with hash-bound reports; native Tk editor panel and web adapters/client with chat, selection/drag, preview, diff, comparison, Apply, history and rollback. Fixed the log-chart edit-context dead code and implemented the previously ineffective `time_exit_only_unfavorable`/`time_exit_min_mfe_pct` timer policies. Added `.env.example` settings and [operation guide](pattern-edit-operation.md).
- Tests: 38 feature tests pass (`core/test_pattern_edit_contracts|versions|service|loader|validation`, `ui/test_pattern_edit_panel`, `web/test_pattern_edit_api`); 81 overlay/paper/web/export tests pass (1 pre-existing failure Q006); parity 5 pass; broader scanner/golden/RSI/execution-accounting 61 pass. New tests: `core/test_pattern_edit_validation.py` (5), `web/test_pattern_edit_api.py` (6), timer-policy and rollback cases.
- In progress: all code and tracker edits are uncommitted on `main`; no commit created. Sandbox-backed candidate execution, live provider smoke and display/two-process runs are not verified.
- Decisions/blockers: D012 (executable `exit_v1` hooks stay contract-only/unreachable). Q001 and Q004 closed; Q002 (delegated cgroup), Q003 (live provider) and Q006 (stale 009 discovery test) remain open. Existing RSI regression is now fixed.
- Next action: P9.4 configured DeepSeek image smoke test, then P9.2 desktop and P9.3 browser/two-process runs after provisioning the Q002 worker.
- Branch/worktree/commit: `main`, `/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22`; no commit created.

### 2026-09-14 — Live verification pass (real data, HTTP, browser)

- Tasks worked: P9.1, P9.3 (browser half), and re-verification of P2/P3/P5/P8 against real data.
- Completed: Wrote `core/test_pattern_edit_integration.py` (create→message→generate→validate→apply→history→rollback, idempotent Apply, sandbox-down safety) and `web/test_pattern_edit_e2e.py` (Playwright). Drove a real 1227-candle paper chart through the whole service flow with a fake runner: all validation checks passed and v2 applied with the source mirror updated. Ran the live `main.py --web` server: login, real chart with `edit_context`, create 200, message 200, preview job fails closed with an actionable provider error and the draft preserved. Browser run mounted the editor on a real closed trade, sent an instruction, and left Apply disabled.
- Bug fixed: `validate_candles` rejected every real chart because it required `low <= open/close <= high`, which real vendor data violates (RPC open>high; CSDX stale sub-penny close<low). Relaxed to the documented invariants (D013/Q007); 15/15 sampled real charts now validate, with a regression test.
- Tests: 128 passed (42 feature) + parity 5; only Q006 (pre-existing stale 009 discovery test) fails. `browser-use` could not complete the CDP handshake in this environment, so the browser run used the project's Playwright harness instead.
- In progress: all code and tracker edits remain uncommitted on `main`. Sandbox-backed execution, live provider and desktop E2E still unverified.
- Decisions/blockers: D013 added; Q007 closed. Q002, Q003, Q006 remain open.
- Next action: provision Q002/Q003, then P9.2 desktop run, P9.3 two-process visibility check, and P9.4 provider smoke test.
- Branch/worktree/commit: `main`, `/home/r00t/codes/trading_bot/trading_bot_v2_swing_jun22`; no commit created.
