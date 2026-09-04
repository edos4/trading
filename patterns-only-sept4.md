# Refactor the Python bot's backtester + paper trader to the `.cjs` pattern-backtest methodology

## Context

`C:\Users\dell\codes\trading` is a Python swing-trading bot. Its **pattern detection is already
faithfully ported** from the Node.js pattern-backtest scripts at `C:\Users\dell\tradingview-mcp`
(`patterns/_channels.py` ≈ `backtest_uc_v14.cjs`, `patterns/002_double_top.py` ≈ the locked
double-top ruleset, `patterns/_rounding.py` ≈ `backtest_rb_v3.cjs`, etc.). What diverges is the
**engine wrapped around the patterns**:

| Concern | `.cjs` method (what we want) | Python bot today |
|---|---|---|
| Data | offline, one JSON/symbol, daily bars, skip `<100` bars | live fetch (`33ai.edos.uk`/Yahoo) + 6h-TTL cache |
| Sizing | flat **$10,000** per trade, independent, no compounding | risk-based (0.75% of 100k / stop distance) |
| Portfolio | none — every trade stands alone | compounding capital ledger, max-concurrent cap, MTM equity curve |
| Entry gates | none beyond the pattern's own conditions | min-confidence, Kronos ML gate, SMA200 regime, post-loss cooldown, min-share-price, long-only, R:R reject |
| Exit | fixed per-pattern ladder: hard stop → target → reclaim → trail → time → data_end | that ladder **plus** engine overlays: `breakeven_stop`, `profit_lock`, `first_bar_invalidation`, `dead_trade_exit`, synthetic ATR stop |
| Metrics | trades / W-L / win-rate% / avg-pnl% / total$ / worst$ / exit histogram (+ PF, ROI) | Sharpe, max drawdown, R multiples, expectancy, account-weighted % |
| Output | `<pattern>_results.json` (`meta`/`summary`/`trades`/`blocked`/`filtered`) + console block | `backtest_results_<ts>.json` via `BacktestResult.to_dict()` |

Backtest and paper **already share an execution core** (`PaperAccount` imports `_open_trade` /
`_check_exit` / `_close_trade` / `_apply_sizing` from `core/backtester.py`). That is the one design
strength to preserve — the refactor swaps the core's *internals*, and paper inherits the change.

**Goal:** the Python backtester and paper trader reproduce the `.cjs` methodology and its
documented per-pattern numbers, on the bot's own offline data cache.

### User decisions (already confirmed)
1. **Strip entirely** every engine overlay — no min-confidence, Kronos, regime, cooldown, ATR stop
   floor, breakeven, profit-lock, first-bar-invalidation, dead-trade, R:R reject, min-share-price,
   long-only. Engine = pattern conditions + pattern exit ladder only.
2. **Flat $10k independent trades.** No risk sizing, no capital ledger, no equity curve, no
   Sharpe/drawdown. Metrics = the `.cjs` `summarize()` set + profit factor.
3. **Own cache** under `codes/trading` — a new builder that fetches via the bot's data layer. Do
   **not** read the `tradingview-mcp` directories at runtime.
4. **All patterns at once**, validated together against the documented numbers.

### Scope resolved
- Nine pattern modules **002–010**; `011_breakout_retest` stays retired. `009_flag` is
  re-enabled. `DISABLED_PATTERNS` becomes empty for the new engine.
- **Golden-number acceptance tests** for the six with a documented `.cjs` figure:
  **002, 004, 006, 008, 009, 010**. **003 / 005 / 007** have no `.cjs` counterpart — port the
  methodology, gate only on "runs, finite trade count".
- `backtest_channel_long_v1.cjs` (long ascending-channel, 238 trades) is **not** a current Python
  pattern → optional follow-up `patterns/012_*`, out of scope for this pass.
- Per-pattern exit constants are reconciled against the **memory condition docs**
  (`upward_channel_conditions.md`, `hs_conditions.md`, `pennant_conditions.md`,
  `rounding_bottom_conditions.md`, `flag_pattern_conditions.md`, `2026-05-04-cvx-double-top-analysis.md`)
  **and** the `.cjs` source — pattern by pattern during implementation, not fully in this plan.
  (Example already spotted: 002's target is `neckline × 0.93` (7%), which the current code already
  has — do not "fix" it to 10%.)

---

## Recommended approach

### 1. Offline bar-cache builder  *(new code, no engine change yet)*

- **Cache dir** `data/barcache/<market>/` (`us`, `ph`), one file per symbol,
  `data/barcache/us/NVDA.json`:
  ```json
  {"symbol":"NVDA","timeframe":"1d","fetched_utc":"…","source":"fetch_ohlcv_candles",
   "bars":[{"t":1690848000,"o":1.0,"h":1.1,"l":0.9,"c":1.05,"v":1234}]}
  ```
  `t` = unix **seconds** UTC session date — the `.cjs` `bar` shape, so ported maths line up.
  Separate from the live scanner's `data/cache/{key}.json` (that stays). **No TTL** — frozen
  research snapshot.
- **`scripts/build_barcache.py`** — CLI (`--market`, `--universe <name|all>`, `--symbols`,
  `--min-bars 100`, `--count 500`, `--refresh`). Per symbol calls
  `data.history.fetch_ohlcv_candles(sym, "1d", market=profile.id)` (`data/history.py:277` — already
  returns `list[OHLCVCandle]`), converts, drops `< min_bars`, writes JSON + a
  `_manifest.json`.
- **`scripts/build_earnings_cache.py`** → `data/barcache/earnings_cache.json`
  `{"NVDA":[unix-sec 8-K/2.02 dates]}`, sourced from `data/edgar_client.py` (add
  `list_earnings_dates(symbol, since, until)` beside the existing `has_earnings_in`). Absent file ⇒
  006 skips its blackout (mirrors `.cjs`).
- **Universes** — `data/universes/<name>.txt` (one ticker/line) + `data.universes.load(name)`.
  Copy the lists **once, by hand** from the `.cjs` sources (never read `tradingview-mcp` at
  runtime): `upward_channel.txt` (~366), `flag.txt` (60 semi/HW/datacenter),
  `pennant.txt` (253), `head_and_shoulders.txt` (~200–440), `double_top.txt` (~200),
  `rounding_bottom.txt` (20), `default.txt` (union). `NAS60` / `DOW` sub-buckets for 006's
  three-way summary → `data/universes/_buckets.py`.
- **Wire into `Backtester`** — add `barcache_dir: str | Path | None` to `__init__`. In `run`'s
  fetch phase (`core/backtester.py:~2152`), when set: `candles = _load_barcache(dir, market, symbol)`;
  `None` or `< min_bars` ⇒ skip the symbol. New `_load_barcache()` parses the JSON,
  `t → datetime.fromtimestamp(t, tz=utc)`. Keep the `ProcessPoolExecutor` (workers already accept
  pre-fetched candles).

### 2. Backtester engine refactor  *(`core/backtester.py`)*

**Delete / gut:** `describe_risk_gate_rejection`, `apply_risk_gates`, `_execution_reward_risk_ok`
(:1148–1269); `_apply_sizing` + `_gap_risk_atr_floor` (replaced); `_apply_capital_ledger` (:1568),
`_build_portfolio_equity_curve` (:572), `_drawdown_pct`, `_realized_daily_returns`;
`_enforce_max_open_positions` (:1536); `_profit_take_level` / `_profit_lock_level` /
`_resolve_profit_lock_trigger_pct`; the breakeven / profit-lock / first-bar-invalidation /
dead-trade blocks inside `_check_exit` (:1028–1128); Kronos + volume-gate + min-confidence +
regime + cooldown + min-share-price + long-only checks in `_core_backtest_symbol` (:1773–1847).

**New `_check_exit(candle, position, bar_idx) -> (price|None, reason)`** — no `ENGINE` reads,
fixed order every bar (mirrors the `.cjs` `for k=entry+1…entry+maxHold` bodies):
1. **Hard stop** — `position.stop_loss`, optionally the nearer of structural and `entry*(1±cap)`
   via new `stop_loss_pct_cap` (the `.cjs` C24 dual stop). Close-based iff `stop_loss_on_close`,
   else intraday with gap-fill at open (reuse `_gap_aware_trigger_fill`).
2. **Target** — `position.take_profit`, close-based, **fill at the target price** (`.cjs`
   `exitPrice = target`).
3. **Reclaim** — only if `position.reclaim_exit` (channels 006/007). New
   `reclaim_lower_rail = (rail_price_at_entry, slope_per_bar)`; exit at close when
   `close > railAt(k)` **and** higher-high **and** higher-low vs the prior bar (track
   `prev_high`/`prev_low` on the position each bar).
4. **Trailing stop** — keep `_trailing_stop_price` / `_update_trailing_reference`; honour
   `trailing_stop_mode` / `_pct` / `_on_close` / `trailing_activation_pct`.
5. **Time stop** — `exit_bars_after_entry` (from fill) or `exit_bars_after_neckline_break` (from
   `neckline_break_bar_idx` / `signal_bar_idx`); return `candle.close`, `"time"`. Drop
   `time_exit_only_unfavorable` / `time_exit_min_mfe_pct` (`.cjs` time stop is unconditional).
6. **Fallback** — still open at window end → `candle.close`, `"data_end"`.

Keep helpers `_gap_aware_trigger_fill`, `_trailing_reference`, `_update_trailing_reference`,
`_trailing_stop_price`, `_update_neckline_state`; add `_effective_stop(position)` for the dual
stop and `_update_prev_hl(position, candle)` for reclaim.

**New sizing — flat notional:**
```python
def _apply_notional_sizing(signal, notional=10_000.0, fractional=False):
    px = signal.price
    if px <= 0: return
    signal.qty = (notional / px) if fractional else max(1, int(notional // px))
```
`fractional=True` for 004 / 009 / 010 (`.cjs` `10000/entry`); floor for 002 / 006 / 008 (and
003 / 005 / 007 by analogy). Floor result `< 1` ⇒ drop the signal (`.cjs` `if (shares < 1) continue`).
Membership set lives in the slim config. Patterns stop setting `qty` themselves.

**New `_core_backtest_symbol` loop** — keep the per-bar `pattern.analyze(snapshot, store)`
convention (paper parity). One open position per symbol; a pattern fires only when
`setup.entry == current`, so a pivot pair anchors on exactly one bar (Python's equivalent of the
`.cjs` `usedSH1/usedSH2` dedup). Entry price = **close of the trigger bar** (already true:
patterns return `price = close.iloc[entry]` and `_open_trade` fills at `candle.close`; its
stop/target rebasing is a no-op when `signal.price == candle.close`). Extend the return to
`(trades, signals, blocked, filtered)` — `blocked`/`filtered` populated **only by 006** (earnings
blackout + C22 freshness + C23 don't-chase).

**Slim `config` dict:** `{position_notional, txn_cost_pct, min_bars(=100),
fractional_qty_patterns, barcache_dir, market, session_tz}`.

**`BacktestResult` / `summarize`** — keep the class name and `trades: list[BacktestTrade]`.
Keep/rename: `win_count`/`loss_count` (win = `pnl_pct > 0`; 0 is a loss), `win_rate_pct`,
`avg_pnl_pct`, `total_usd` (`Σ pnl*qty`), `worst_usd`, `by_exit_reason`, `profit_factor`
(gross win $ / gross loss $ on `pnl*qty`), `payoff_ratio`, `roi_on_deployed`. Delete:
`equity_curve`, `initial_capital`, `capital_rejected`, `max_drawdown_pct`, `sharpe_ratio`,
`account_weighted_pnl_pct`, `final_capital`, `avg_r`, `median_r`, `expectancy_pct`. Keep
`pattern_breakdown()` trimmed to those fields; optional
`summarize(trades, buckets={"nas60":…, "dow":…})` for the 006 run.

**Output** — `to_dict()` → `{meta{date,version,symbols,conditions,market,notional}, summary{…,
nas60, dow}, trades[{sym,pattern,pivot dates/prices/RSIs,entryDate,entryPrice,shares,exitDate,
exitPrice,exitReason,daysHeld,pnlPct,pnlUSD}], blocked[…], filtered[…]}`. `summary()` → the
`.cjs` console block. `main.py` writes `backtest_<pattern>_<ts>.json` + `.txt`.

**txn cost / slippage / lot rounding / weekly:** `txn_cost_pct` default **0.0** (documented
headline numbers carry no cost line); `--txn-cost 0.001` matches `.cjs` "cost optional"; PH
overlays `profile.txn_cost_pct`. **No slippage anywhere.** `apply_lot_rounding` kept PH-only
(`if get_market(market).lot_round`). `_derive_weekly_from_daily` left dormant (no pattern uses
`1W`).

### 3. `core/engine_defaults.py` → slim shared config

Keep the module path and the name `ENGINE`. Replace the ~40-field `EngineDefaults` with:
```python
@dataclass(frozen=True)
class EngineConfig:
    position_notional: float = 10_000.0
    txn_cost_pct: float = 0.0
    min_bars: int = 100
    fractional_qty_patterns: frozenset[str] = frozenset({
        "pattern_004_rounding_bottom", "pattern_009_flag_pattern", "pattern_010_pennant"})
ENGINE = EngineConfig()
```
Delete `EngineDefaults`, `REGIME_REQUIRED_PATTERNS`, `structure_filters_enabled`,
`regime_filter_required`, `backtest_kwargs`, `risk_gate_kwargs`, `sizing_kwargs`, and every
`passes_* / describe_*` gate helper. `grep -rn "engine_defaults"` to find the fix-up wave (all on
the delete path). `main.py` builds `Backtester(...)` directly instead of via `backtest_kwargs`.
Keep `core/market.py MarketProfile` (PH still needs long-only + lot rounding + its own costs — it
just inherits the same slim engine).

### 4. Pattern touch-ups  *(exit fields only — no detection-geometry changes)*

New `TradeSignal` fields (`patterns/base_pattern.py`):
```python
stop_loss_pct_cap: float | None = None            # dual stop: nearer(structural, entry*(1±cap))
reclaim_exit: bool = False
reclaim_lower_rail: tuple[float, float] | None = None   # (rail price at entry, slope per bar)
```
**Bug to fix first:** `004_rounding_bottom.py:61` and `005_rounding_top.py:61` pass
`protective_exit_on_close=True` — **not a field on `TradeSignal`** → `TypeError` the moment either
pattern detects a setup. Replace with `stop_loss_on_close=True` / the RB dual-stop expression.

Per-pattern reconciliation (against memory docs + `.cjs`):
- **002 double_top** → locked ruleset: `take_profit = neckline*0.93`, `exit_bars_after_neckline_break=5`,
  C15 3% trailing checked on **intraday high** vs lowest-close×1.03
  (`trailing_stop_mode="lowest_close"`, `trailing_stop_on_close=False`). Verify the current file
  already matches; adjust only what doesn't.
- **004 rounding_bottom** → `stop_loss = price*0.95` close-based; `take_profit = price + 0.80*(neckline-price)`;
  `trailing_stop_pct=0.15`, `trailing_stop_mode="highest_high"`, `trailing_stop_on_close=True`;
  dual stop = `max(fixed, trailing level)` (already what `_first_adverse_protective_fill` does);
  fractional shares; **no time stop**; open → MTM at data_end.
- **006 upward_channel** → add C24 `stop_loss_pct_cap=0.05`; C21 `reclaim_exit=True` +
  `reclaim_lower_rail=(setup.entry_line, setup.slope)`; trailing → close-based
  (`trailing_stop_on_close=True`, keep 2.5% / 4% activation); C22 (`return None` if
  break_bar − SH2_bar > 20) + C23 (`return None` if `(SH2_price − entry)/SH2_price > 0.15`) in
  `analyze()`; switch the earnings blackout to read `data/barcache/earnings_cache.json`.
- **007 descending_channel** → mirror of 006 inverted (no golden number).
- **008 head_and_shoulders** → add close-based invalidation stop at right-shoulder close
  (`stop_loss = rs_close`, `stop_loss_on_close=True`); trailing already close-based 3%;
  `exit_bars_after_entry=10`; measured-move target filled at target price.
- **009 flag** → already close: `stop_loss = max(flag_low, price*0.97)` intraday, ratcheting 3%
  trailing off highest close intraday; fractional shares; re-enable in `DISABLED_PATTERNS`.
- **010 pennant** → trailing already matches (5% close-based off running extreme); add
  `exit_bars_after_entry=60` → `timeout`; fractional shares.
- **003 double_bottom / 005 rounding_top** → fix the `protective_exit_on_close` bug (005), strip
  engine reliance, sanity-gate only.

**RSI parity:** `IndicatorEngine.rsi()` is a simple rolling mean; `.cjs calcRSI14` is Wilder
(SMA seed → Wilder smoothing), which `IndicatorEngine.rsi_wilder()` matches. 004 / 008 already use
`rsi_wilder`. **Switch 002 and 006 (and the `_channels` / `_rounding` call sites) to
`rsi_wilder`** so gate thresholds (`RSI ≥ 55`, `≤ 61`, divergence ≥ 5) line up with the `.cjs`.

**Right-margin guard:** add to each `analyze()` the `.cjs` loop-bound rule — `return None` if
`current > len(df) - (confirm_window + max_hold + lb)` — so patterns too near data-end are never
anchored (mechanical, matches `.cjs`, no geometry change).

### 5. Paper trader refactor  *(`core/paper_trader.py`)*

Because `PaperAccount` already imports the core functions, the new ladder is inherited →
**automatic backtest/paper parity**. Changes:
- `_apply_sizing(...)` → `_apply_notional_sizing(signal, ENGINE.position_notional,
  fractional=signal.pattern in ENGINE.fractional_qty_patterns)`.
- **Delete** cash-affordability cap, gross-exposure cap, `max_open_positions` /
  `max_open_per_pattern`, daily-loss limit (+ `_daily_pnl` / `_reset_daily_if_needed`), slippage
  (entry + exit), `min_position_notional`, breakeven/profit-lock/profit-take assignment on the
  position, `_execution_reward_risk_ok` call, `_resolve_profit_lock_trigger_pct`.
- **Keep** the scan loop, live + stream feed, `tick()` / `bar_count()`, `position_marks`, JSON
  persistence (`data/cache/paper_account.json` per market), `may_assume_fill` session gating
  (PH), lot rounding (PH). `_on_bar_locked` still calls `_check_exit(candle, position, bar_idx)`
  (new signature, no kwargs). Multi-position independent $10k; `self.cash` bookkeeping becomes
  cosmetic. `to_result()` → `BacktestResult(trades=list(self.closed))`, emitting the `.cjs` block.
- **`core/scanner.py`** — `_process_signal` (:948) and `_finish_signal` (:1183) collapse to
  "for each new bar, for each pattern, `analyze()` → if signal, `_apply_notional_sizing` →
  `paper.open_position()`". Delete the whole gate gauntlet, the volume-gate block, the vision
  block, `_kronos_then_finish` / `_run_kronos_rank_sleeve` / `_pending_kronos*`, and collect-first
  (`_collect_pool` / `_flush_collect_first` — `signal_reward_risk` is deleted with
  `engine_defaults`). `MarketScanner.__init__` loses `kronos_gate` / `kronos_rank` /
  `volume_gate` / `collect_first*` / `pattern_only`.

### 6. Kronos & vision — sever, keep on disk

Delete every Kronos import/call from `core/backtester.py` and `core/scanner.py`; **keep**
`core/kronos_*.py` and any standalone `main.py` kronos subcommand. `analysis/vision_checker.py`
stays; delete only the `settings.vision_confirmation_enabled` block + `_run_vision_check` call
from the scanner. Also sever `analysis/price_volume.volume_confirm_gate` from the money path
(keep `compute_volume_metrics` only if the `rvol`/`obv_slope` JSON columns are still wanted —
otherwise drop those fields from `BacktestTrade`). Check `core/paper_books.py` /
`core/pattern_jobs.py` for ledger/sizing assumptions and trim.

### 7. Callers & tests

- **`main.py`** — `run_backtest` / `run_paper` / `run_scanner` / `_parse_args`: drop
  `--kronos-gate`, `--volume-gate`, `--volume-gate-compare`, `--pattern-only`,
  `--collect-first*`; add `--barcache DIR`, `--universe NAME`, `--txn-cost FLOAT`. Delete the
  `volume_gate_compare` A/B block (`main.py:467–528`).
- **`ui/backtest_dialog.py PARAMS`** (:44) + **`web/jobs.py`** — remove every risk/gate/overlay
  row; keep `market`, `n_symbols`/`universe`, `extra_symbols`, `pattern_filter`, `max_workers`;
  add `barcache_dir`, `txn_cost_pct`. `web/jobs.py` iterates `PARAMS` generically — drop the
  hardcoded `"hard_stop_percentage"` at `web/jobs.py:96`.
- **Tests** — delete `core/test_risk_gates.py`, `core/test_engine_defaults.py`,
  `core/test_volume_gate.py`, `core/test_pattern_only.py`, `core/test_kronos_*` (unless the
  standalone CLI is kept). Rewrite `core/test_execution_accounting.py` (flat $10k:
  `qty == floor(10000/entry)` or fractional; `pnl_usd == shares*(entry−exit)` short /
  `shares*(exit−entry)` long; txn 0; no ledger) and `core/test_paper_trader.py` (drop
  max-positions / daily-loss / affordability / slippage; add "N independent $10k positions" +
  "paper `_check_exit` reason == backtester `_check_exit` reason on the same synthetic bars").
  **Add** `tests/test_golden_numbers.py` (param per pattern, runs `Backtester` over a **committed
  fixture barcache** under `tests/fixtures/barcache/`, asserts trade count / win rate / net $
  within tolerance — §Verification), `tests/test_backtest_paper_parity.py`,
  `tests/test_barcache_builder.py`, `tests/test_rsi_matches_cjs.py`.

### 8. Work order

1. `data/universes/*.txt` + `data.universes.load` (copy lists from `.cjs`).
2. `scripts/build_barcache.py` + `_load_barcache` + `scripts/build_earnings_cache.py`; build
   `data/barcache/us/`; `tests/test_barcache_builder.py`.
3. Slim `engine_defaults.py` → `EngineConfig`; fix the import-error wave by deletion.
4. `TradeSignal` new fields + fix the `protective_exit_on_close` bug (004/005).
5. Backtester core: new `_check_exit`, `_apply_notional_sizing`, new `_core_backtest_symbol`,
   strip `__init__`/`run`/`config`, new `BacktestResult` + `summarize` + JSON/console,
   `barcache_dir`.
6. Pattern touch-ups one at a time — each followed by its single-pattern backtest over
   `data/barcache` and a diff vs the `.cjs` number.
7. Paper trader → scanner → Kronos/vision sever.
8. Callers: `main.py`, `ui/backtest_dialog.py`, `web/jobs.py`.
9. Tests: delete / rewrite / add.
10. Verification pass.
11. *(optional)* `patterns/012_ascending_channel_long.py` from `backtest_channel_long_v1.cjs`.

---

## Critical files

| File | Role in the refactor |
|---|---|
| `core/backtester.py` | `_check_exit` (:995), `_core_backtest_symbol` (:1697), `_apply_sizing` (:1433), `_open_trade`/`_close_trade` (:1272/:1382), `BacktestResult` (:152), `Backtester.run` (:2134); delete `apply_risk_gates` / `_apply_capital_ledger` / `_enforce_max_open_positions` / `_build_portfolio_equity_curve` |
| `core/engine_defaults.py` | collapse `EngineDefaults`→`EngineConfig`; delete all gate helpers + `*_kwargs` |
| `core/paper_trader.py` | `_open_position_locked` (:481), `_on_bar_locked` (:689), `to_result` (:837) |
| `core/scanner.py` | `_process_signal` (:948), `_finish_signal` (:1183); delete gauntlet + Kronos + collect-first |
| `patterns/base_pattern.py` | `TradeSignal` — new `stop_loss_pct_cap` / `reclaim_exit` / `reclaim_lower_rail` |
| `patterns/002,004,005,006,007,008,009,010_*.py` | exit-field reconciliation; RSI→Wilder for 002/006; right-margin guard; fix `protective_exit_on_close` (004/005) |
| `patterns/_channels.py`, `_rounding.py`, `_rules.py` | pass `rsi_wilder`; `earnings_blackout` reads the new earnings cache |
| `data/history.py` | `fetch_ohlcv_candles` (:277) — the builder's data source |
| `data/edgar_client.py` | add `list_earnings_dates()` |
| `main.py` | `run_backtest` (:406), `run_paper` (:215), `_parse_args` (:559) |
| **new** | `scripts/build_barcache.py`, `scripts/build_earnings_cache.py`, `data/universes/*.txt`, `data/universes/_buckets.py`, `tests/test_golden_numbers.py`, `tests/fixtures/barcache/` |

---

## Verification

### Per-pattern (the acceptance bar)
```bash
python main.py --backtest --pattern pattern_006_upward_channel --barcache data/barcache --universe upward_channel --txn-cost 0
```
Compare the console `summarize` block to the documented figures:

| Pattern | Target (documented `.cjs`) | Tolerance |
|---|---|---|
| 006 upward_channel | 127 trades, 55.9% WR, +$23,717, worst −$774 | ±10% trades, ±3pt WR, ±15% net $ |
| 002 double_top | 7 patterns, ~85.7% trade WR, avg +3.81% | small sample — same names ⇒ near-exact |
| 008 head_and_shoulders | 19 trades, 63% WR, +$3,876 | ±10% / ±3pt / ±15% |
| 010 pennant | 41 trades, 61.0% WR, +$23,494 | order-of-magnitude (25–55 trades, 50–70% WR) |
| 004 rounding_bottom | 2 trades, 100% WR, +$8,943 | exact if the fixture uses the same 2 names |
| 009 flag | 108 trades, 43.5% WR, +1.99%/trade, PF 2.44 | ±10% / ±3pt / ±15% |
| 003 / 005 / 007 | none | "runs, finite trade count, no crash" |

Then a **combined run** (`--universe default`, no `--pattern`) as a sanity check only — one
position per symbol across all patterns means it will not equal the sum of per-pattern runs.

### Backtest == paper
Backtest pattern P over `data/barcache`; then replay the same bars through paper
(`python main.py --paper --stream --stream-start <first-bar-date> --reset --pattern P`, fed from
the barcache) and confirm identical trades/exits. `tests/test_backtest_paper_parity.py` automates
a small case.

### Expected drift (document, don't chase)
- **Data source** — `.cjs` caches were built from TradingView desktop; Python uses
  `33ai.edos.uk`/Yahoo. Split/adjust and last-bar differences shift pivots ±1–2 bars → a few
  trades appear/disappear. **Pin the fixture barcache in git** so golden tests are deterministic.
- **Per-bar `analyze()` vs `.cjs` full-history pivot scan** — usually lands on the same
  last-completed pattern (both iterate most-recent-first); add a `setup_key` + per-symbol
  `used_keys` set only if duplicate trades show up.
- **RSI seeding** — resolved by moving 002/006 to `rsi_wilder`; borderline gate flips
  (`RSI` right on 55 / 61) are acceptable. `tests/test_rsi_matches_cjs.py` guards the formula.
- **Pennant / rounding-bottom are two-stage in `.cjs`** (curated pattern list from a separate
  historical scan) — largest divergence; treat 010/004 as order-of-magnitude unless the fixture
  universe is seeded from exactly the `.cjs` pattern tickers.
- If a golden test can't hit tolerance after the fixture is pinned, **diff the two bar series for
  the diverging symbol before touching pattern code** — it's almost always the data source.

---

## Implementation status (2026-09-04)

Branch `refactor/cjs-backtest-methodology`.

### Done — engine + plumbing (runs end-to-end)
- `data/universes/` — 7 ticker lists copied from the `.cjs` sources + `_buckets.py` (NAS60).
- `data/barcache.py` + `scripts/build_barcache.py` (live) + `scripts/import_cjs_barcache.py`
  (one-time seed). `data/barcache/us/` seeded from the `.cjs` `barcache/`+`flagcache/`
  (489 symbols) + `earnings_cache.json`. `data/barcache_flag/` holds the flag universe's
  500-bar tapes.
- `core/engine_defaults.py` → slim `EngineConfig` (`position_notional`, `txn_cost_pct`,
  `min_bars`, `fractional_qty_patterns`). All gate helpers / `*_kwargs` deleted (2 no-op
  shims kept for the Explorer UI).
- `core/backtester.py` fully rewritten: flat-notional sizing, fixed per-pattern exit ladder
  (`_check_exit`: hard/dual stop → target → reclaim → trail → time → data_end), offline
  barcache loading, `.cjs`-style `BacktestResult.summarize` + `to_dict` + console block,
  `blocked`/`filtered` diagnostics, `end_margin` right-guard. Kept `_open_trade` /
  `_close_trade` / helpers shared with paper.
- `core/paper_trader.py` — notional sizing, stripped portfolio caps / daily-loss / gross
  exposure / cash-affordability / slippage-default / R:R / breakeven / profit-lock;
  `to_result()` emits the `.cjs` block; tolerant load for old ledgers.
- `core/scanner.py` — gate gauntlet + Kronos + collect-first + vision severed; signal path
  is `analyze() → _apply_notional_sizing → paper.open_position`.
- `main.py` — `run_backtest` rewritten (`--barcache` / `--universe` / `--txn-cost`; per-
  pattern universe default); legacy A/B path removed.
- `ui/backtest_dialog.py`, `web/jobs.py`, `web/app.py` — slim PARAMS, universe-driven,
  volume A/B retired.
- Patterns: 002/003/006/007 → Wilder RSI; 004/005 `protective_exit_on_close` **bug fixed**;
  006 → C21 reclaim + C22/C23 filters + C24 dual stop + offline earnings cache; 008 → RS
  invalidation stop + notional qty; 010 → 60-bar timeout. 009 already matched.
- Tests: obsolete suites deleted (`test_risk_gates`, `test_engine_defaults`,
  `test_volume_gate`, `test_pattern_only`, `test_kronos_gate`, `test_kronos_rank_sleeve`);
  `test_execution_accounting` rewritten; obsolete cases dropped from `test_paper_trader` /
  `test_market` / `test_scanner_deferred_entry` / `test_services_patterns`.
  **211 pass**; remaining failures are pre-existing/environmental (`web/` needs the MCP
  server, which isn't installed here — those suites could not even *collect* before this
  branch; `analysis/test_chart_viewer_payload` failed on `main` too).

### Not done — per-pattern numerical parity (the bulk of remaining work)
The engine runs, but the ported pattern *detection* does not yet reproduce the documented
`.cjs` trade counts / win rates. First measured run:

| Pattern | `.cjs` (verified live) | Python now |
|---|---|---|
| 006 upward_channel | 127 trades / 55.9% WR / +$23,717 / worst −$774 | 62 / 33.9% / −$6,038 / −$498 |

Needs symbol-by-symbol reconciliation of `patterns/_channels.py` / `_rounding.py` / the
double-top / H&S / flag / pennant geometry against the `.cjs` scan — comparing which
pivot pair each side picks, RSI seeding at the gate thresholds, and exit-fill conventions
(`.cjs` UC exits stop/target at the *close*, HS/DT/RB at the *level*). `end_margin`
(currently 5) also needs tuning against the `.cjs` loop-bound (~48 bars reserved on SH1).

### Follow-ups
- `tests/test_golden_numbers.py` + pinned `tests/fixtures/barcache/` once a pattern hits
  tolerance.
- `tests/test_backtest_paper_parity.py`.
- Rework `web/` PARAMS schema JSON + templates for the slim form (functional, not polished).
- Optional `patterns/012_ascending_channel_long.py` from `backtest_channel_long_v1.cjs`.
