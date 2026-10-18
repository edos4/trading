# Backtest and paper-stream correctness plan

Status: software changes for steps 1–6 and the step 7 evaluation safeguards are implemented. The final affected suite passed all 400 tests with no skips. Actual untouched-data evaluation remains an operator task, recorded below.

Based on the [2026-10-16 review](backtest-paper-video-review-2026-10-16.md) and its reproduced failures. The objective is causal, reproducible backtests that match paper-stream decisions under the same configuration, with honest execution and account metrics. This does not establish profitability.

## Scope and decisions

- Use `StreamReplay` as the canonical strategy-evaluation path. Keep the existing offline engine only for explicitly labeled legacy replication; do not present its results as causal validation.
- Reuse shared trade math, pattern versions, frozen datasets, job storage, and existing tests. Add no optimizer, dependency, or second execution engine just to fix these defects.
- Preserve historical results and existing paper books. Execution changes require a new runtime identity and explicit compatibility handling, not rewriting old trades.
- Preserve the launcher's volume gate and top-four selection when implementing paper/backtest parity. Do not silently disable them to make results match.
- Keep independent fixed-notional research supported and clearly labeled. Finite-account execution is a separate requirement before claiming deployable account returns.

Before each implementation step, inspect current callers, fixtures, configuration, exports, and existing uncommitted changes. The review describes a working tree, not an immutable release.

## 1. Establish regression tests and result labels

Relevant files: `core/backtester.py`, `core/backtest_service.py`, `core/backtest_params.py`, `tests/test_backtest_paper_parity.py`, `core/test_stream_backtest.py`, and web/desktop/CLI consumers of these settings and results.

- [x] Add small deterministic regression cases for future-dependent detection, more than 512 input bars, gap-through stops, same-bar trailing updates, entry timing, and open-position fees. Keep each test next to the behavior it protects.
- [x] Identify legacy offline results as historical replication in result metadata and consuming interfaces. Make historical-stream the normal evaluation choice wherever supported; fail explicitly for unsupported combinations.
- [x] Retain the current parity test as a legacy helper/fill test, but rename or document its scope: its paper side receives full history and historical-walk state.

Acceptance: users can distinguish legacy replication, causal research replay, and finite-account simulation. Existing legacy golden numbers are not silently relabeled as causal results.

## 2. Fix history alignment and enforce causal detection

Relevant files: `core/backtester.py::_core_backtest_symbol`, `core/stream_backtest.py`, `data/ohlcv_store.py`, `patterns/002_double_top.py`, other evaluated detectors, and `patterns/test_live_detection.py`.

- [x] Give the retained legacy adapter enough store capacity for its entire input tape so original walk indices address the same candles. Keep this local; do not enlarge every live store globally.
- [x] Verify a 600-bar input at the first tradable bar, the 512-bar boundary, and the final scanned bar. Assert matching timestamps and prices, not merely absence of exceptions.
- [x] Keep canonical evaluation on progressively supplied bars without `_dedup.set_current` historical-mode detection. Audit each enabled detector for confirmation delays and references beyond the decision bar.
- [x] Exercise the double-top case where a later neckline break previously authorized an earlier entry. Mutating future bars must not change earlier signals or entry decisions.
- [x] Align paper/replay history-window and anchor-identity semantics, including rolling-window eviction. Test beyond the live window so a setup cannot disappear, reappear, or be duplicated solely because indices shifted.

Acceptance: no evaluated signal depends on a future candle. Future-mutation tests compare decisions made before the cutoff; later exits and P&L may legitimately change. Legacy tape alignment is correct even though that mode remains explicitly noncausal.

## 3. Correct exit execution without regressing Patternsv2

Relevant files: `core/backtester.py::_check_exit`, `_gap_aware_trigger_fill`, trailing helpers, `core/paper_trader.py`, `core/pattern_provenance.py`, and version/runtime compatibility checks.

- [x] Use gap-aware execution for resting legacy stops. A long stop at 95 on OHLC 90/92/85/88 fills at the opening 90 before configured slippage, not 95.
- [x] Establish close-based trailing levels only after checking the current bar against previously known levels. A close of 110 must not create a 104.5 stop that executes against an earlier low of 99.
- [x] Distinguish resting orders from close-confirmed exits. A close condition cannot execute retroactively at a better trigger price; define its next executable fill consistently with step 4.
- [x] Document conservative ordering when both stop and target are touched and OHLC cannot establish the sequence. Preserve valid opening-price precedence.
- [x] Test long/short gaps, target touches, both-trigger bars, activation, ratcheting, and save/reload behavior. Retain Patternsv2's existing correct cases.
- [x] Introduce the changed execution semantics under a new runtime identity. Do not add old engine hashes to a compatibility allowlist when exit behavior actually changed. Define supported continuation or explicit migration of open books before rollout.

Acceptance: pre-slippage fills are executable under the documented model, trailing updates use only available information, and old books cannot silently acquire different exit rules.

## 4. Execute close-confirmed entries at the next opportunity

Relevant files: `core/scanner.py::_apply_bar` and `_finish_signal`, `core/paper_trader.py`, `core/stream_backtest.py`, paper persistence, signal/trade contracts, and export consumers.

- [x] Queue a close-confirmed candidate instead of opening it at the completed signal close. Retain its signal timestamp, version, gate evidence, anchor, and intended rules.
- [x] Fill historical replay at the next eligible bar's open with side-correct slippage. Recalculate quantity/board lots and validate stop/target geometry against the actual fill. Specify whether structural levels remain fixed or are rebased; use the same rule in both paths.
- [x] Process pending opening fills before that bar's subsequent stop/target checks. Include entry-day exits and opening gaps in tests.
- [x] Distinguish historical paper replay from a live feed. A live scanner that only receives completed daily bars cannot claim it traded an opening price it observed hours later. Use an available executable observation or label the result as retrospective simulation.
- [x] Persist pending entries and their consumed state so restart, repeated bars, or transient failures cannot duplicate or lose them. Revalidate eligibility at execution, including existing Laya expiry/recheck rules where enabled.
- [x] Define expiration, missing-session behavior, cancellation, and end-of-data handling. A signal with no subsequent eligible execution bar remains unfilled and is reported as such.

Acceptance: close 100 followed by open 110 produces entry 110 plus slippage, never 100. Replay and paper use matching event order, and pending orders survive restart exactly once.

## 5. Charge fees when incurred and reconcile all metrics

Relevant files: `core/paper_trader.py` entry/exit/cash handling, `core/backtester.py::_close_trade`, `core/stream_backtest.py::_mark_equity` and `_artifacts`, `core/backtest_service.py::_build_result`, and `utils/trade_export.py`.

- [x] Deduct the entry commission at entry for both longs and shorts; charge only the exit commission at exit. Keep closed-trade net P&L inclusive of both legs.
- [x] Reflect incurred entry costs in open-book equity and net P&L. Define realized/unrealized/fee reporting consistently so consumers do not subtract the same cost twice.
- [x] Reconcile account cash, position value, equity curve, fee totals, exported trades, and final net P&L for keep-open and force-close policies.
- [x] Version or explicitly migrate saved books that have open positions under the old deferred-fee convention. Loading must not double charge fees or change historical closed trades.

Acceptance: a flat-price $10,000 entry at 0.1% costs $10 immediately; at a flat-price exit total cost is $20. Long/short, restart, partial book closure, and force-close tests reconcile to the same totals.

## 6. Match the paper launcher's decision pipeline

Relevant files: `paper_stream.sh`, `core/scanner.py` collect-first and gate processing, `core/backtest_service.py::validate_settings`, `core/stream_backtest.py`, and settings/contracts/frontends.

- [x] Capture the effective configuration: symbol set, history window, versions, market/calendar, volume gate, collection size, ranking/tie-break rules, costs, entry policy, and enabled optional gates.
- [x] Add replay support for the launcher's collect-first selection using the existing ranking behavior. Collect candidates across the session before selecting the top four; preserve the scanner's ordering of ranking, gates, and execution.
- [x] Reuse a small shared selection function only if necessary to prevent duplicated ranking logic. Reject unsupported optional gates instead of omitting them silently.
- [x] Add end-to-end parity fixtures that feed the same bars progressively through actual scanner/paper behavior and replay, including multiple symbols, more than four candidates, gate rejection, gaps, restart, and open positions at the end.
- [x] Compare selected/rejected signals, fills, quantities, reasons, fees, and equity. Normalize only nondeterministic IDs and wall-clock fields; preserve simulated decision/fill timestamps.

Acceptance: the equivalent of `--volume-gate --collect-first=4` produces the same decisions and ledger under both paths. Do not execute the existing reset-enabled launcher against a user's book to test this.

## 7. Add honest evaluation and account boundaries

- [ ] Start with an untouched chronological holdout for a frozen strategy. Record dataset identity, symbols, version/runtime hashes, execution settings, selection date, and evaluation boundaries in existing run artifacts.
- [ ] If parameters are tuned, use successive training windows followed by unseen test windows. Select only on training data, freeze before each test, and aggregate only nonoverlapping test-period returns. Do not add parameter search for a fixed strategy that does not need it.
- [x] Allow earlier candles for indicator warmup without counting their trades as test returns. Prevent training labels or trades extending into the test interval from leaking outcomes; define boundary position handling explicitly.
- [ ] Reserve a final holdout that is not reused to choose revised strategies. Report fees, exposure, drawdown, trade count, and open-position valuation alongside return.
- [x] Label uncapped fixed-notional results as independent-trade research. Before offering finite-account results, implement and share explicit affordability, exposure, short-margin/borrow assumptions, and deterministic order priority across replay and paper.
- [x] Document whether the universe is point-in-time and whether prices include corporate-action adjustments. If delisted symbols or historical membership are unavailable, state that survivorship bias has not been eliminated.

Acceptance: reports distinguish causal replay from unseen-data validation and independent-trade research from executable account performance. No claim of a profitable edge follows from test passage alone.

## Verification and rollout

Implement steps 1–6 in small reviewable changes; execution and fee changes must include compatibility decisions before release. Step 7 governs interpretation of the corrected results.

- [x] Run focused regression tests for each change, then the affected paper, stream, scanner, Patternsv2, service/frontend, export, and parity suites.
- [x] Use an isolated UTF-8 PostgreSQL database via `PATTERN_EDITOR_TEST_DATABASE_URL`; never derive test access from the application's production settings. Critical database-dependent checks must run, not skip.
- [x] Run deterministic long/short fixtures and a frozen representative multi-symbol tape. Compare repeated runs and paper/replay outputs under identical settings.
- [x] Verify cancellation, reconnect, duplicated bars, pending-order restart, pinned runtime handling, and book reload without reset.
- [x] Record corrected results as new runs. Preserve prior results with their legacy semantics and explain expected changes in trade counts, entries, exits, fees, and returns.
- [x] Update user-facing defaults and documentation after verifying the execution contract and compatibility behavior.
- [ ] Operator: re-evaluate strategies on untouched data rather than treating changed golden values as evidence of improvement.

The review's 73 passing selected tests are a baseline, not acceptance of these fixes. Completion requires the new failure cases to pass and the matched scanner/replay comparison to succeed.

## Not included

No production deployment, live orders, automatic paper-book reset, broad architecture rewrite, new ML model, or optimizer is authorized by this plan. Real broker execution and profitability remain unverified even after software checks pass.

## Implementation log

- 2026-10-16: Labeled legacy summaries/exports and web/desktop mode selection; historical-stream is the interactive default. Explicit API calls without a mode retain compatibility and still resolve to offline. Fixed the legacy store capacity locally. Added deterministic history-alignment and corrected-exit regression tests.
- In progress: versioned causal exit rules, persistent next-open orders, entry-fee accounting, and shared collect-first selection. Existing saved trades default to legacy semantics; no saved results or books have been rewritten. Tests and compatibility checks are being completed before marking these steps done.

### 2026-10-17 progress

- Steps 1–2: legacy tape alignment is covered with 600 bars. Historical-stream feeds only received bars, uses the same bounded history as paper, and identifies consumed anchors by timestamp rather than shifting indices. Replay-window bounds now participate in the frozen input hash; changing the interval cannot reuse a different interval's result.
- Step 3: new paper/replay trades use `causal-v1`. Resting orders fill at the opening price when gapped through; otherwise protective stops take precedence on ambiguous OHLC bars. Trailing references update after management. Close-confirmed stops, time/reclaim exits, and indicator flips queue for the next open.
- Compatibility: trades without an execution-model field retain the original legacy ladder. The existing legacy-only compatibility hashes were not broadened for the corrected engine. Unknown pinned runtimes still require restoring their archived compatible runtime; they are not automatically migrated. New causal trades and pending entries reject an obsolete execution hash. Old open positions keep zero `entry_fee_paid` and their deferred-fee convention; new entries record and pay the entry fee immediately. Loading/saving does not rewrite historical fills or charge fees a second time.
- Step 4: pending signals, assessment evidence, consumed anchors and explicit order outcomes survive save/reload. Fills use the next observed bar open with side-correct slippage and size/board-lot recalculation. Structural stop/target levels stay fixed; openings past them are rejected. The entry session's range can stop the position immediately. A missing tape expires an entry at its next observation after seven calendar days (14 for weekly signals). End-of-data candidates remain unfilled; stopping paper preserves its pending queue for resume, whereas cancelling a replay produces no completed result.
- Closed-bar live feeds reject new entries because they have no executable live quote. Retrospective next-open fills require historical replay. Queued orders are logged as queued and do not increment filled-trade counts. Laya filter decisions must be reassessed at execution; errors reject the new entry without interrupting exits. The execution recheck receives only the regular-session opening observation, not that session's later high/low/close or volume.
- Step 5: open equity and net unrealized dollars include incurred entry commissions. Closed P&L includes both commissions; fee totals are informational and must not be subtracted again. Final position marks, exports, account cash and closed P&L reconcile. Price-change percentages and R multiples remain gross price measures.
- Step 6: restored the scanner's requested volume/collect-first flags, which were previously hard-disabled. Replay and scanner share selection ordering: best R:R per symbol/timeframe, then R:R, confidence and symbol. Select top-N before applying the volume gate; a selected veto does not backfill. A six-symbol parity fixture compares queued/rejected/filled decisions, entries, quantities, commissions and equity. Unsupported vision confirmation, non-fixed sizing, Kronos gates, and Laya+collect-first combinations fail explicitly.
- Causal detector audit: the channel detectors' earnings blackout used future 8-K occurrence dates without historical announcement timestamps. Causal mode now reports `Point-in-time earnings calendar unavailable` and blocks those candidates. Legacy historical mode keeps the original cache lookup. A point-in-time calendar is required before these channel candidates can trade causally.
- Focused checks so far: 74 passed across correctness, holdout, Patternsv2, causal detector, Laya, recovery, accounting and exports; 66 passed across web backtest, paper-book and paper API checks. Counts overlap with later runs and must not be added. Additional cases now cover long/short entry-day stops, old-book fees, strict runtime refusal and execution-only observations; final test evidence follows below.

### Evaluation workflow (step 7)

`BacktestSettings.evaluation` and the web/MCP run request accept an optional declaration, for example:

```json
{
  "purpose": "holdout",
  "training_end": "2024-12-31",
  "strategy_frozen_at": "2026-10-17T00:00:00+00:00",
  "selection_note": "Record exactly how the frozen strategy was chosen without examining this test interval",
  "final_holdout_start": "2026-01-01"
}
```

Use historical-stream with explicit start/end dates after `training_end`, and `force-close` at the test boundary. Development folds must end before the reserved final holdout. For a final evaluation use `purpose: final-holdout` and a window beginning at or after that reservation. The dates above illustrate the contract; they are not a claim that these dates were untouched.

Fit/select parameters externally using training outcomes ending before each test starts, pin the selected version, then submit the test request through the existing service. No optimizer was added for fixed strategies. `core.backtest_evaluation.stitch_test_folds([service.result(run_id), ...])` rejects overlapping test windows, open boundary positions and mixing the final holdout with development folds. It excludes warmup points and combines test P&L at constant initial notional, without claiming compounded finite-account returns.

Artifacts retain the protocol declaration, frozen input/dataset/runtime identities, symbol set and costs. A declaration is marked `operator-declared`; ordinary runs remain `unverified` for holdout purposes. Neither code nor this session can establish whether an operator has already seen a dataset or tuned on it. Universe membership, delisting coverage and corporate-action adjustments are explicitly unverified. Finite-account limits, short borrowing and margin remain outside the offered independent-trade model.

Operator actions still required for step 7: choose a genuinely untouched interval, record the strategy selection evidence, and run that frozen experiment once. Do not reclassify previously inspected development results as unseen tests.

### Configuration and reporting details

- RVOL and OBV thresholds are frozen in execution parameters and passed explicitly to the volume gate. A legacy volume-gated preset without these values must be saved again before execution. The scanner captures the same thresholds at startup.
- Interactive forms default to historical-stream, the startup market's per-leg commission, and a warmup matching the configured historical stream's initial lookback minus the decision bar. Explicit API requests retain their prior defaults for compatibility. Match costs, slippage, initial lookback/warmup and detector versions when comparing runs. Warmup can be set above 400 bars through the API and form.
- Signals without natural pattern anchors use their decision-bar timestamp for deduplication; they can generate fresh orders on later sessions. Multiple prints of one daily session use the final print once.
- Existing drawdown percentages are peak dollar losses divided by initial capital, rather than peak-relative percentage drawdowns. The stitched fold report names this explicitly as `drawdown_pct_of_initial`. Exposure is reported in currency notional in replay logs and as position values in open-position artifacts.
- The Laya opening observation uses the regular 09:30 local-market opening time and zero current-session volume. Unusual exchange opening schedules require a market-calendar extension before that execution model is used for them.

### Test evidence

- [Focused implementation suite](verification/fix-backtest-focused-tests.txt): 122 passed before final integration. This includes causal exits, long/short fees, persistence, late fills, holdout boundaries, detector causality, scanner parity and export checks.
- [Final affected suite](verification/fix-backtest-tests.txt): **400 passed in 203.27 seconds, no failures or skips**, with source held unchanged against an isolated UTF-8 PostgreSQL database. It covers paper/replay, service, frontend, pattern versions/loaders/contracts, complete bootstrap parity, Patternsv2, causal detectors, Laya, recovery, accounting, exports, indicators, CLI and MCP callers. No application database, saved paper book, production registry, or reset-enabled launcher was used.

Final verification (2026-10-17): `git diff --check`, Python compilation and `node --check web/static/backtest_runs.js` passed. Existing uncommitted work was preserved. No commit, deployment, production reset or live trade was performed. Step 7's unchecked items require the operator's actual untouched-data selection/experiment; the implemented contract and fold aggregation cannot certify that past data was unseen.

### Requested execution smoke (2026-10-17)

- [x] Run actual cached market data through historical backtest and isolated paper scanner/account paths, both baseline and volume-gated with top-four enabled.
- [x] Assert equal fills, fees and all 260 daily equity marks, including warmup. Both runs passed; baseline had 11 closed and 1 open trade, gated had 1 closed trade.
- [x] Preserve saved paper books and record reproducible artifacts. See [run details, metrics and limitations](verification/backtest-paper-smoke-2026-10-17/README.md). This is an offline historical paper replay; live feed/broker connectivity and untouched-data evaluation remain unverified.
