# Directional Imbalance Index integration plan

Status: local implementation completed on 2026-10-26; automated verification passes.
Calculation, Builder catalog, rule consumers, and default Paper/Replay chart display
are implemented. Manual browser checks, database-backed save/load/preview, and
external TradingView parity remain unverified. No rule or book was activated.

Integrate BigBeluga's Directional Imbalance Index as a locally computed, multi-output entry in the existing visual rule catalog. Use it first as an optional context filter through the Builder's existing shadow/filter controls. A saved Builder rule can also trade it independently once its entry and exit policy has been evaluated.

The initial release must also show DII when a user double-clicks an open or closed trade row on the **Paper** or **Replay** page. Chart visibility is required even when the trade came from another pattern and no DII rule or filter is enabled.

The supplied Pine script defines an oscillator and chart decorations. It supplies no trading strategy, protective stop, target, or alert condition. Adding it directly to automatic detector discovery would require inventing those decisions and would change every local Indicators-only scan. The catalog gives users explicit control with substantially less integration work.

**Where it fits today**

| Existing code | Integration role |
| --- | --- |
| `analysis/indicator_engine.py` | Shared local OHLCV calculations. Add the DII calculation here. |
| `analysis/rule_catalog.py` | Registers parameters, outputs, labels, validation, and warmup. Add one catalog entry and a thin compute adapter. |
| `analysis/rule_engine.py` | Already compares outputs, supports offsets and crossings, and supplies per-condition evidence. Reuse it. |
| `web/rules.py`, `web/static/builder.js` | `/api/rules/catalog` drives the Builder's indicator, parameter, and output selectors. A catalog entry should appear without DII-specific frontend code. |
| `analysis/rule_detector.py` | `VisualRuleIndicator` already turns a saved rule's false-to-true transition into a `TradeSignal`, using its configured risk plan. |
| `core/scanner.py` | `rule_id` runs a saved rule independently. `rule_filter_id`, `rule_filter_mode`, and `rule_filter_position` attach a rule to existing signals. |
| `core/rule_backtest.py` | Existing saved-rule preview and replay path; use within the limitations below. |
| `analysis/chart_renderer.py:build_trade_viewer_payload()` | Shared interactive payload builder for Paper and Replay row charts. Compute and attach DII series here. |
| `core/paper_books.py`, `web/app.py` | Paper trade chart history and `/api/paper/chart`; Replay history and `/api/replay/chart`. Both feed the shared payload builder. |
| `web/static/tv_chart.js`, `web/static/app.js` | Shared renderer and row double-click/modal handlers. Add the DII pane, price overlays, and hover readout to both pages. |
| `indicators/__init__.py`, `indicators/_signal.py` | Discover trade-emitting `BaseIndicator` subclasses. No new subclass is needed for the initial integration. |
| `data/tv_studies.py`, `data/tv_stacks.py` | Existing authenticated remote-study path. DII can compute entirely from local high/low history, so it needs no TradingView session or stack. |

The initial data path is:

```text
closed OHLCV history
  -> IndicatorEngine.directional_imbalance_index(length, period)
  -> rule_catalog.compute("directional_imbalance_index", ...)
  -> rule_engine: comparisons and evidence
  -> saved rule used as a scanner filter or VisualRuleIndicator
  -> existing paper execution and position management
```

**Calculation contract**

Keep the source's defaults: `length=10`, `period=70`. For chronological bar `t`:

```text
upper[t] = max(high[t-length], ..., high[t])
lower[t] = min(low[t-length], ..., low[t])

up_event[t]   = 1 if high[t] == upper[t], else 0
down_event[t] = 1 if low[t]  == lower[t], else 0

up[t]   = sum(up_event[t-period+1], ..., up_event[t])
down[t] = sum(down_event[t-period+1], ..., down_event[t])
total[t] = up[t] + down[t]
bulls[t] = 100 * up[t]   / total[t]
bears[t] = 100 * down[t] / total[t]
```

Implementation details that affect parity:

- The extrema window is **`length + 1`**, including the current bar. A 10-bar rolling maximum would be an off-by-one error.
- Every event compares a bar with that bar's own trailing extrema. Do not compare all 70 historical highs with today's `upper`.
- Equality counts. A repeated high or low can contribute again; this is not a strict breakout test. Compare the original numeric series without tick rounding or approximate equality.
- Both events can occur on one bar. `up + down` can exceed `period`; each individual count remains between zero and `period`.
- The arrays seeded with zero are recreated each bar because they are not declared `var`. Their sums are rolling event counts, not lifetime cumulative counts, and the zero seed adds nothing.
- Percentages divide by the combined event count, not by `period`. They describe relative event frequency, not the probability that a trade succeeds.
- If `total == 0`, retain zero counts and return `NaN` percentages. A prolonged sequence of contracting ranges can produce this case. Do not manufacture a 50/50 reading or forward-fill an earlier reading.
- `showBul`, `showBer`, colors, label arrays, fills, and `barstate.islast` blocks only control presentation. They do not alter the numerical result.
- The pasted Markdown links and escaped angle brackets/asterisks are formatting artifacts. Normalize them only when preparing a Pine reference fixture; do not treat them as source syntax.

Use pandas rolling maxima/minima, boolean event series, and rolling sums. This avoids the source's repeated per-bar loop and uses already installed dependencies. Keep calculation deterministic and free of network, store, database, and execution side effects.

**Warmup and missing data**

Adopt a deliberately conservative trading contract: full finite high/low windows are required. Use `min_periods=length+1` for extrema and `min_periods=period` for event sums. Preserve invalid events as `NaN`; converting comparisons against unavailable extrema directly into zero would incorrectly make the count window appear ready.

For zero-based rows, extrema first become valid at row `length`; counts first become valid at row `length + period - 1`. Therefore catalog warmup is **`length + period` candles**, or **80** at defaults. `required_bars()` currently adds two bars and applies a 60-bar floor, so an unshifted default DII rule requests **82** candles. Reuse that policy rather than adding a second warmup mechanism.

Return extrema once their own window is valid, but mask both counts and percentages until the full event window is valid. A nonfinite high or low invalidates the paired event for that bar and any dependent count window. Do not drop candle rows, interpolate, or count missing values as failed events. Preserve output length and chronological alignment. Holidays absent from the candle series do not create extra bars; these are candle counts, not calendar-day windows.

Full-window startup and fail-closed handling of corrupt candles are explicit bot policies. Do not claim parity with TradingView's early dataset bars or its treatment of missing inputs. Numerical parity is required on finite, fully warmed windows. An empty or short frame returns aligned empty/`NaN` outputs without emitting a trade.

The calculation is causal: appending future candles must never change prior outputs. The original script can update on a forming candle, and `barstate.islast` does not mean closed. For trading, use the scanner's existing completed-session-bar checks; compare TradingView values on confirmed bars. See [TradingView's bar-state documentation](https://www.tradingview.com/pine-script-docs/concepts/bar-states/).

**Public interface and catalog entry**

Add `IndicatorEngine.directional_imbalance_index(length=10, period=70) -> dict[str, pd.Series]`, following the engine's existing RangeIndex convention. Its calculation depends only on high and low. Validate positive integer arguments at this callable boundary; the catalog separately resolves and validates API inputs using its existing convention.

Register:

| Property | Proposed value |
| --- | --- |
| ID | `directional_imbalance_index` |
| Label | `Directional Imbalance Index [BigBeluga]` |
| Group | `Trend` |
| `length` | Integer; default 10; initial catalog range 1–250 |
| `period` | Integer; default 70; initial catalog range 1–250 |
| Ordered outputs | `bulls`, `bears`, `up`, `down`, `upper`, `lower` |
| Warmup | `int(p["length"]) + int(p["period"])` |

All outputs use the catalog's float-series contract. Counts remain numerically integral. Document percentage fields as 0–100, count fields as 0–`period`, and extrema as prices. Put `bulls` first so the Builder's existing implicit first-output behavior resolves to a useful percentage, and use explicit fields in saved examples.

The proposed parameter ceilings are local UI limits, not a reproduction of Pine's input limits. Their maximum combined warmup plus the engine's two-bar allowance is 502, within the default 512-candle store. Actual fetched history may be shorter: `settings.tv_history_days` defaults to 450. Nonzero operand offsets add more history requirements. Preserve the existing insufficient-history result and document it; do not silently shorten windows or raise global history settings for this indicator.

**How to use it**

Start with a daily, long-side context rule such as `bulls > bears` (equivalently `bulls > 50` when the total is positive). An optional stronger candidate is `bulls >= 60`. These are evaluation examples, not thresholds supplied by BigBeluga or established profitable settings. A balanced flat series produces 50/50 because ties count, so strict dominance excludes that case. Strong relative dominance can still arise from very few events; it is not independently a measure of trend strength or liquidity.

For a pattern filter, evaluate the persistent state with `gt`/`gte`, not a one-bar crossing. Existing scanner integration provides:

- `rule_filter_mode="shadow"`, `rule_filter_position="after"`: record evidence on candidate signals without vetoing them. This is the recommended first run.
- `rule_filter_mode="filter"`, `rule_filter_position="after"`: veto candidates when the saved rule is false or unavailable.
- Patternsv2 additionally supports `position="before"` with filter mode to skip targeted detection. Before placement does not support shadow mode. Start after placement so candidate evidence remains visible.

Scope the filter to the intended pattern and timeframe. A saved rule has one declared direction; it does not automatically invert bullish conditions for SELL candidates. Use explicitly bearish rules (`bears > bulls`) only in appropriately scoped short-side evaluation. A filter gates new entries; it does not replace the pattern's exit policy.

For an optional independent strategy, the existing `VisualRuleIndicator` can run the same persistent-state rule. It enters only on a false-to-true transition. Avoid pairing a `crosses_above` pulse with `exit.on_false=true`: the pulse normally becomes false on the next bar even when bullish dominance persists. Decide the stop, target, and rule-false exit behavior explicitly through `ExitPlan`; the source supplies none of them. Keep PH's existing long-only restriction and the paper book's capital/position limits.

This is a complete example payload for validating, saving, and previewing a daily long rule. The risk settings are illustrative and require evaluation before use:

```json
{
  "schema_version": 1,
  "name": "DII bullish context",
  "timeframe": "1d",
  "direction": "long",
  "root": {
    "kind": "group",
    "op": "AND",
    "children": [{
      "kind": "condition",
      "left": {
        "source": "indicator",
        "id": "directional_imbalance_index",
        "params": {"length": 10, "period": 70},
        "field": "bulls",
        "offset": 0
      },
      "operator": "gt",
      "right": {"source": "value", "value": 50}
    }]
  },
  "exit": {
    "on_false": true,
    "stop": {"kind": "percent", "percent": 5},
    "target": {"kind": "r_multiple", "r": 2}
  }
}
```

Save through the existing `/api/rules` route, then select the saved rule as a paper filter or pass its ID as `rule_id` for independent paper evaluation. Do not seed a database row or activate a book automatically. Pure math and catalog use need no database; saving rules uses the existing rules PostgreSQL setup.

**Replay limitations to handle before drawing conclusions**

Source inspection found several existing constraints. They do not prevent adding the calculation and shadow evidence, but they constrain validation:

- `core/rule_backtest.py:load_candles()` always loads `1d`. Preview and backtest do not currently honor a rule's weekly timeframe. Start evaluation with `1d`. Before advertising weekly preview/backtest, load actual completed weekly candles or explicitly reject weekly requests. Never label daily results as weekly. The calculation itself remains valid for correctly supplied weekly candles.
- `_replay()` calls `_bracket(..., df)` with the entire frame; ATR extraction takes the last ATR value. ATR stop plans can therefore use future history. Before evaluating an ATR-based DII strategy, compute its risk inputs from the decision-bar prefix, freeze the intended distance, and compare next-open execution with paper. The percentage example above avoids that particular dependency.
- Current scanner paper entries and rule replay both queue decisions for the next observed bar open. Old test descriptions claiming signal-close entry are not the execution contract. Verify actual fills, gaps, and bracket handling with fixtures.
- Rule replay skips stop/target checks on the entry bar and keeps fixed brackets, whereas a rule with `on_false=true` enables `VisualRuleIndicator.trail()` and the scanner's stop ratchet. Match these behaviors, fees, slippage, and rule-false exit timing before claiming replay/paper P&L parity.
- A rule-only replay evaluates the DII strategy; it does not measure its effect on Patternsv2 candidates. Measure a pattern filter with paired runs of the same pattern pipeline and universe, first in shadow mode, then with the gate enabled.

Keep any execution fixes separate and covered by focused causal/parity tests. They are prerequisites for affected performance claims, not reasons to rewrite the execution engine as part of the indicator calculation.

**Implementation plan**

Phases 1–3 are implemented; phase 4 documentation and automated checks are complete, with manual UI and database checks pending. Phase 5 covers evaluation after that release; its execution-engine fixes apply only when the affected capabilities are included. The initial deliverable is calculation, Builder availability, verified rule consumption, and visible DII on Paper and Replay row charts. Phase 3 includes the required chart work. Enabling a trading rule remains a separate choice.

| Phase | Files to change | Deliverable | Depends on |
| --- | --- | --- | --- |
| 1. Calculation | `analysis/indicator_engine.py`; new `analysis/test_directional_imbalance_index.py` | Deterministic six-output method with numerical and causal tests | Calculation contract above |
| 2. Catalog | `analysis/rule_catalog.py`; `analysis/test_rule_engine.py` | Builder metadata, parameter resolution, output comparisons, and warmup | Phase 1 |
| 3. Consumers and row charts | `analysis/chart_renderer.py`; `web/static/tv_chart.js`; `web/static/app.js`; chart callers if needed; consumer and chart tests listed below | DII visible on Paper/Replay charts, plus verified API, detector, filter, and daily replay consumption | Phase 2 |
| 4. Release documentation and UI check | `README.md`; this document | Usage instructions, recorded checks, and explicit remaining limitations | Phase 3 |
| 5. Controlled evaluation | Attributed parity fixture; evaluation results; affected replay code/tests only if needed | TradingView comparison and evidence for optional strategy/filter activation | Phase 4 and the relevant data/permissions |

**Phase 1 — implement and verify the calculation**

- [x] Add `directional_imbalance_index()` beside the existing indicator calculations. Preserve the defaults and return keys defined above. Include BigBeluga attribution, the license link, and a note describing the stricter startup/missing-data policy in its docstring.
- [x] Validate direct-call arguments as positive integers, rejecting booleans, fractional values, zero, and negative values with a clear `ValueError`. Keep the catalog's separate input-resolution convention unchanged.
- [x] Convert high/low to local float series and normalize nonfinite inputs to `NaN` without mutating the engine's source frame.
- [x] Calculate rolling extrema with `window=length+1` and full-window `min_periods`. Build a shared event-validity mask requiring both extrema and the current high/low to be finite.
- [x] Convert exact high/upper and low/lower comparisons to float event series, then apply the validity mask with `.where(valid)`. Roll those events over `period` with full-window `min_periods`; missing events must remain missing until their dependent window expires.
- [x] Replace a zero combined count with `NaN` only in the percentage denominator. Return both count series unchanged, plus the percentages and extrema, in the declared output order.
- [x] Add an independent loop oracle in the new numerical test file and cover the test matrix below. Include assertions that the input frame is unchanged, output indexes and lengths agree, and appending or truncating history preserves the expected values.

Acceptance: the method matches the loop oracle on fully warmed finite windows; at defaults the first valid count/percentage row is 79; zero-total percentages and invalid windows remain unavailable; all six series are causal. Run the new numerical test file before proceeding.

**Phase 2 — expose DII through the catalog**

- [x] Add a thin `_directional_imbalance_index(df, p)` adapter that calls the engine method with resolved `length` and `period`. Keep all calculation logic in the engine method.
- [x] Add one `Indicator` entry to `INDICATORS` using the proposed ID, label, `Trend` group, parameter ranges, output order, and `length + period` warmup. Existing `_BY_ID` and `catalog_payload()` construction should pick it up automatically.
- [x] Extend rule-engine tests for omitted parameters, both explicit parameters, all six explicit output fields, implicit first-output selection, and rejection of unknown parameters/fields or values outside catalog bounds.
- [x] Verify a `bulls > bears` comparison and a `bulls > 50` condition agree when the denominator is positive. Verify missing percentages never pass a percentage comparison, including `ne`.
- [x] Assert catalog warmup is 80 and `required_bars()` is 82 at defaults. Cover a nondefault window, an operand offset, and one crossing condition to catch alignment mistakes.

Acceptance: `catalog_payload()` contains exactly one DII entry, existing indicators retain their metadata and behavior, and generic rule evaluation consumes the returned series. No rule schema, saved-rule migration, or frontend-specific DII branch is needed.

**Phase 3 — verify consumers and implement Paper/Replay charts**

- [x] In `web/test_rules_api.py`, add authenticated catalog and validation cases outside database-dependent test markers. Assert the defaults, bounds, and six outputs, and validate the example payload through the API. Reject a bad DII field or parameter through the same route.
- [x] In `analysis/test_rule_detector.py`, inject a daily saved DII rule and a deterministic candle sequence that transitions from nonmatching to matching after warmup. Assert one BUY, no duplicate BUY while the state remains true, `visual_rule` provenance, finite protective risk levels, and condition evidence containing the computed percentage.
- [x] Exercise `trail()` while the persistent state matches and after it stops matching. Check that `exit.on_false` controls trailing participation using the existing contract; do not implement DII-specific exit logic.
- [x] In `core/test_scanner_rule_prefilter.py`, inject the same rule and mocked history. Assert after-placement shadow mode records evidence without rejecting a candidate, filter mode vetoes false/unavailable context, and insufficient history does not invent a match. Exercise the real post-signal gate for shadow/veto behavior rather than only asserting its evidence helper.
- [x] In `core/test_rule_backtest.py`, use daily fixtures and a percentage stop to verify a DII transition leads to entry on the next observed bar's open, never on the signal bar. Keep this test's claim limited to entry timing; it does not establish complete replay/paper P&L parity.
- [ ] With the existing rules test database available, add or extend save/load/preview coverage for the DII payload. Assert preview evidence agrees with direct catalog computation on the same candle prefix. Mark any unavailable database check as not run.

The chart work below is required in this phase:

- [x] Extend `build_trade_viewer_payload()` with a `dii` object containing `length`, `period`, and time-aligned `up`, `down`, `bulls`, `bears`, `upper`, and `lower` series. Use the same engine method as the rule catalog. Emit finite values or chart whitespace points; never serialize `NaN`/infinity as JSON numbers.
- [x] Calculate on available normalized, deduplicated history **before** visible-range/trade-window cropping, then slice outputs to emitted candle timestamps. Preserve missing high/low values during calculation so the indicator's invalid-window policy still applies. `_prepare_df()` currently removes invalid price rows: account for that ordering when preparing the computation frame, rather than silently compressing its history. Preserve existing candle validation and deduplication behavior.
- [x] Trace both callers: Paper's `PaperBook.render_trade_chart()` and Replay's `api_replay_chart()` must supply preceding history when available. Preserve timeframe resampling and market timezone. Do not restart the oscillator at entry or compute only on visible candles. A short scanner tape may need the existing history source for warmup; do not add remote-study computation.
- [x] Render a DII count pane in `web/static/tv_chart.js`, alongside the existing price/volume and RSI panes. Plot `up` in orange `#FB8C00` and `down` in blue `#2962FF`, with count units and a legend titled `DII (10, 70)` for the initial default display. Put `upper` and `lower` on the price chart in their corresponding colors. Do not put counts or percentages on the price scale or apply RSI's fixed 0–100 scale to counts.
- [x] Extend Paper and Replay hover/readout paths in `web/static/app.js` to show counts and bull/bear percentages for the cursor's bar. Hovering any pane must refer to the same candle. Undefined percentages show an unavailable value, not 0% or the last valid reading. Without hover, label any summary as the last displayed bar's value.
- [x] Extend lifecycle handling for the additional pane: synchronized pan/zoom and crosshair time, aligned plot widths, resizing, cleanup on close/remount, and `capture()` including all visible panes. Preserve entry/exit markers, pattern/trend lines, correction tools, and saved drawing identities. DII is read-only and needs no new drawing-tool mode.
- [x] Extend `analysis/test_chart_viewer_payload.py` for DII values, pre-crop warmup, invalid-input alignment, session deduplication, and weekly timestamps. Assert future candles cannot change an earlier bar's DII. Extend `web/test_replay_flow.py` and applicable chart tests in `web/test_paper_api.py`/`core/test_paper_books.py` to prove both chart routes carry DII for ordinary pattern trades.
- [x] Extend `web/test_replay_client.cjs` and `web/test_trendline_geometry.cjs`, adding a focused shared chart test if necessary, for the additional pane, synchronization, capture, and cleanup. Test payloads without `dii` so older stored chart/correction contexts still render.

Acceptance: focused tests pass without a TradingView connection. Double-clicking an open or closed row on either page renders the DII pane and price bands without enabling a DII strategy. Database-free checks run independently of rules storage. Rule API/scanner/detector production code changes only for a demonstrated integration defect; chart payload and renderer changes are explicitly part of this release.

**Phase 4 — document and check the initial release**

- [x] Update the README's Builder documentation with the DII name, defaults, fields/units, 80-candle calculation warmup, 82-candle default rule requirement, daily usage example, and license attribution.
- [x] Run the focused command below. Record pass/fail/skip counts and any unavailable external checks in this document; do not describe planned checks as completed.
- [ ] In an existing local Builder session, select DII, edit both parameters, switch output fields, and compare preview evidence with the engine's values. Save/reload the example rule if rules storage is configured. If the UI needs a fix, keep it generic to catalog-driven indicators.
- [ ] Browser-check Paper and Replay separately: double-click open and closed rows, confirm DII counts and price bands, hover all panes, pan/zoom, resize, capture, close, and open a different row. Include ordinary pattern trades, short history, and a weekly chart. Confirm RSI, annotations, and corrections still work.
- [x] Update this document's status to reflect the implemented scope and list remaining parity, weekly, replay, or chart work explicitly. Existing books and saved rule selections should retain their prior configuration.

Acceptance: a user can see DII on Paper and Replay row charts and use it in the Builder. The initial release includes shared chart payload, renderer/readout changes, focused tests, and documentation; no new dependency or configuration key is introduced. Chart visibility does not depend on rules storage.

**Recorded verification — 2026-10-26**

- Focused pytest command below: **160 passed, 3 skipped, 0 failed**. Skips are
  `WebRulesApiTests.test_catalog_and_crud`, `test_dii_save_load_preview`, and
  `test_corrupt_list_row_does_not_hide_healthy_rows`; no
  `RULES_TEST_DATABASE_URL` was configured. DII save/load/preview coverage is
  present but was not run against PostgreSQL. Catalog/validation HTTP checks
  ran independently of storage.
- Numerical file alone: **43 passed**. This is algorithm verification against
  the independent loop oracle, not TradingView parity.
- `node web/test_replay_client.cjs`, `node web/test_trendline_geometry.cjs`, and
  `node web/test_chart_draw.cjs`: **all passed**. Coverage includes older payloads
  without DII, unavailable hover values, three-pane time/width alignment,
  crosshair time, resize, stacked capture, and cleanup.
- Paper open/closed pattern-trade chart routes and Replay open/closed chart
  routes carry DII without a selected DII rule. Weekly payloads, session
  deduplication, invalid input alignment, pre-crop warmup, and causality passed.
- Browser checks were **not run**: the in-app browser connection reported that
  `iab` was unavailable. Builder parameter editing/field switching, visual
  inspection of Paper/Replay modals, and interaction/capture QA remain pending.
- Follow-up visual regression: all three frontend scripts passed with assertions
  for orange/blue crossing fills, gaps, latest percentages, event labels, and
  capture readout inclusion. No narrative arrows or trend claims are generated.
- No TradingView connection/export or paired trading evaluation was used.
  Phase 5 remains pending. Weekly rule replay and the documented execution
  discrepancies are unchanged; no replay/paper P&L parity is claimed.

**Phase 5 — validate optional trading use**

- [ ] Obtain a confirmed-bar TradingView export and add an attributed fixture with symbol, exchange, timeframe, session, adjustment settings, input parameters, export precision, and source provenance. Run the numerical comparison described below.
- [ ] Before offering weekly preview/backtest, make `load_candles()` timeframe-aware and use completed weekly bars, or reject weekly requests explicitly. Add a daily-versus-weekly fixture proving that daily history cannot be reported as weekly results.
- [ ] Before evaluating ATR-based entries, remove the full-frame ATR dependency in replay. Freeze the selected risk inputs from the signal-bar prefix and test that modifying future candles cannot change earlier stops or entries.
- [ ] Before claiming strategy replay/paper parity, align and test entry-bar bracket checks, stop ratcheting, rule-false exits, gaps, fees, and slippage in the existing shared execution paths. Record any remaining differences rather than treating the basic DII replay test as sufficient.
- [ ] For filter evaluation, run paired daily pattern scans with the same universe, date range, costs, and pattern settings. Start with after-placement shadow mode; record accepted/rejected candidate counts and their DII evidence. Evaluate return, drawdown, and trade-count changes on a separate holdout period before choosing a threshold.
- [ ] Enable a filter or independent strategy only through an explicitly selected saved rule and configured paper run. Satisfy the license requirements below for the intended use. Do not automatically apply the example threshold to existing books.

Acceptance: numerical parity is supported by a reproducible fixture; affected performance comparisons use causal, matching execution assumptions; any activated rule and scope are explicit. To roll back a trial, remove the rule/filter selection from the affected book. Keep the calculation available so saved DII rules remain readable and valid.

**Verification matrix**

| Test case | Required result after warmup |
| --- | --- |
| Strictly rising highs and lows | `up=period`, `down=0`, `bulls=100`, `bears=0` |
| Strictly falling highs and lows | `up=0`, `down=period`, `bulls=0`, `bears=100` |
| Constant high/low | Both counts equal `period`; percentages are 50/50 |
| Repeated extrema and outside bars | Equal highs/lows count again; a bar can increment both sides |
| Prolonged strictly contracting ranges | Counts can both become zero; percentages are `NaN`; percentage rules do not match |
| Small hand-worked sequence | Exact historical-window alignment; a departing event leaves the count at the right bar |
| `length=1`, `period=1`, plus nondefault settings | Correct inclusive window and first valid row |
| Empty/short frame; internal `NaN`/infinity | Aligned outputs, no invented events or trade-ready percentages |
| Prefix vs full-history calculation | Every available output through the prefix end is unchanged |
| Rolling store truncation with enough history | Latest outputs equal those computed on the full history |

For independent TradingView parity, prepare an attributed Pine reference with temporary numeric plots for `bulls`/`bears` (the original exposes those as labels) and export high, low, `Up`, `Down`, `upper`, `lower`, and percentages. Use identical symbol, exchange, timeframe, session, adjustment settings, and confirmed bar timestamps. Record the settings and fixture provenance. Compare fully warmed finite windows: counts exactly, extrema to the same input precision, percentages within `1e-9` when full precision is available, otherwise within the documented export rounding. Do not round production math to force a match. Until that export is available, call the loop-oracle checks algorithm verification rather than TradingView parity.

Suggested focused command after implementation:

```bash
.venv/bin/python -m pytest -q \
  analysis/test_directional_imbalance_index.py \
  analysis/test_rule_engine.py analysis/test_rule_detector.py \
  core/test_rule_backtest.py core/test_scanner_rule_prefilter.py \
  analysis/test_chart_viewer_payload.py core/test_paper_books.py \
  web/test_rules_api.py web/test_paper_api.py web/test_replay_flow.py
```

Run affected frontend regressions after the chart changes:

```bash
node web/test_replay_client.cjs
node web/test_trendline_geometry.cjs
node web/test_chart_draw.cjs
```

**Required chart presentation and scope**

Double-clicking Paper or Replay rows must use the existing chart modal and show DII by default, including trades produced by unrelated patterns. The initial display uses fixed defaults `length=10`, `period=70`, independently of saved rule parameters. Label those settings clearly so a differently configured trading rule is not mistaken for the displayed indicator. A chart parameter editor and historical rule-parameter snapshots are outside this initial display change.

Paper open/closed rows and Replay open/closed rows are all required. Paper signal-log charts using the same builder should also receive the display. Use each chart's actual timeframe, session timezone, and supplied history; weekly chart display is supported independently of the weekly rule-replay limitation above. Per-bar DII values use only that bar and its history. Preserve replay cutoff metadata and the distinction between inspection of later bars and evidence available at entry; do not present the final chart value as the entry-time signal.

Display three areas: the existing price/volume chart with DII upper/lower overlays, the existing RSI pane when present, and the new DII count pane. Retain readable heights, with scrolling or a larger modal if needed on smaller screens. With insufficient history for a full 80-candle calculation window, keep the chart open and show an explicit warmup/unavailable state in the DII area. Other chart content remains usable; individual unavailable readings create gaps. The follow-up visual update adds orange/blue gradient fill between the count lines, latest-bar percentage labels, and `+1` price labels over the displayed trailing calculation period. DII grid lines and dotted last-price guides are hidden, and the count pane is taller. The count pane, percentages, and price overlays remain required.

The initial implementation requires no new dependency, `.env` setting, Pine interpreter, remote-study request, database migration, broker integration, or automatic `BaseIndicator` registration. It also adds no global regime gate, optimization framework, smoothing, volume weighting, or inferred stop based on `upper`/`lower`. Those would change the product or the trading policy beyond translating this indicator.

**Attribution and permitted use**

The supplied source credits **© BigBeluga** and states **CC BY-NC-SA 4.0**. Preserve that attribution and license URL in the method's documentation and any reference fixture, identify the Python translation and startup/missing-data changes, and retain the [original TradingView publication](https://www.tradingview.com/script/WSvRZ0j0-Directional-Imbalance-Index-BigBeluga/), supplied in the follow-up review. The publication describes rolling extreme-event counts, relative percentages, price bands, event labels, and dominance fill. The supplied Pine script remains the calculation reference; a confirmed-bar export is still needed for independent numerical parity. Shared adaptations must follow the applicable attribution and ShareAlike terms. The license also restricts commercial use. See the [official license summary](https://creativecommons.org/licenses/by-nc-sa/4.0/) and [legal code](https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.en).

Before deploying a port for commercial trading or distributing it within a commercial product, establish the applicable rights or obtain separate permission from the author. Do not assume a Python translation or paper-trading mode removes the NonCommercial restriction. This plan records the supplied license; it does not establish commercial permission.

Completion means a tested local calculation appears in the Builder, saved rules expose its actual values and warmup, existing shadow/filter/strategy paths consume it without changing default trading behavior, and double-clicking Paper or Replay trade rows shows the DII pane, percentages, and price overlays. TradingView numerical parity and any profitability claim require their separate evidence described above.
