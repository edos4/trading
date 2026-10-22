# Visual Indicator Editor

**Status:** implemented (2026-10-22). See the audit notes at the end for the
source-level and browser checks.

**Goal:** a new **Builder** tab where a user creates a named indicator ("indicator1") from
draggable condition blocks, saves it to PostgreSQL, and uses it two ways:

1. **As a strategy** — run it as its own paper book (and replay-backtest it).
2. **As a filter** — gate pattern / patterns_v2 signals, so a trade only fires when the
   rule's context says the market is in the right state (the "no context before it finds
   the pattern" / "catching sideways movement" complaint).

---

## 1. What exists today (constraints the design must fit)

| Concern | Reality | Consequence |
|---|---|---|
| Web stack | **FastAPI + Jinja2**, single `create_app()` in `web/app.py`, no routers/blueprints. Feature routes live in `web/<feature>.py` as `register_<feature>_routes(app, templates, ctx)` (`web/tasks.py:83`, `web/laya.py:142`, `web/kronos_history.py:98`). | Follow the register-module pattern; one nav link in `base.html:13-24`. |
| Frontend | Vanilla JS, no build step, no framework. SortableJS **already vendored** (`web/static/vendor/sortable.min.js`, used by the Kanban board, `web/static/tasks.js:472-490`). | Drag/drop needs **no new dependency**. |
| Indicator math | `analysis/indicator_engine.py` (pandas in/out): sma, wma, rsi, rsi_wilder, macd, stoch, atr, bollinger, obv, vwap. **No EMA, no ADX, no RVOL.** | Add a thin catalog layer; implement EMA/ADX/RVOL there. |
| Detector contract | `BaseIndicator` (`indicators/_signal.py:20`) + `emit()` (`:82`). `TvStackIndicator` (`data/tv_stacks.py:379-471`) is the existing precedent for a **JSON-configured** detector plugged into the live scanner. | A rule detector can be a `BaseIndicator` subclass injected exactly like a TV stack. |
| Paper path | `PaperStartRequest.tv_stack_id` → `web/app.py:_start_book` (`:1117`) → `PaperBook.start` (`core/paper_books.py:502`) → `MarketScanner` (`core/scanner.py:163`) → `_discover_patterns` (`:1688`). | Mirror `tv_stack_id` for `rule_id`; no engine surgery. |
| Signal gates | `_finish_signal` (`core/scanner.py:1381`): volume gate → vision → Laya → queue. | Insert the rule filter right after the volume gate. |
| DB convention | `core/<feature>_db.py` + `migrations/<schema>/NNNN_*.sql` + `scripts/setup_<feature>_db.py` + `core/<feature>_store.py`, psycopg3, no ORM. Tests skip without `<FEATURE>_TEST_DATABASE_URL`. | Copy the quartet verbatim (kronos is the newest example). |
| Editable entities | JSONB `payload` + `generation` optimistic concurrency (`core/backtest_service.py:113-136`, `migrations/pattern_editor/0001_registry.sql:87-90`). | Rules are JSONB documents with a generation counter. |
| Backtester | `core/backtester.py` is coupled to **pinned DB pattern versions** (`_load_patterns:959-970` requires `version:` specs). | Do **not** bend the main Backtester. Give rules their own small replay (`core/rule_backtest.py`). |

Naming: the existing `/indicators` tab is the TradingView-study editor. The new feature is
the **Builder** tab at `/builder`; the user-facing object is still called an "indicator",
rows live in `indicator_rules`.

---

## 2. Data model

### 2.1 Rule payload (the JSONB document)

```jsonc
{
  "schema_version": 1,
  "name": "indicator1",
  "timeframe": "1d",
  "direction": "long",                 // "long" | "short"
  "root": {                            // always a group
    "kind": "group",
    "op": "AND",                       // "AND" | "OR"
    "children": [
      {
        "kind": "condition",
        "left":  {"source": "indicator", "id": "rsi", "params": {"period": 14}, "field": "value", "offset": 0},
        "operator": "gte",             // gt|gte|lt|lte|eq|ne|crosses_above|crosses_below
        "right": {"source": "value", "value": 30}
      },
      {
        "kind": "group",
        "op": "OR",
        "children": [
          {"kind": "condition",
           "left":  {"source": "indicator", "id": "close", "params": {}, "field": "value", "offset": 0},
           "operator": "crosses_above",
           "right": {"source": "indicator", "id": "sma", "params": {"period": 50}, "field": "value", "offset": 0}},
          {"kind": "condition",
           "left":  {"source": "indicator", "id": "adx", "params": {"period": 14}, "field": "value", "offset": 0},
           "operator": "gte",
           "right": {"source": "value", "value": 20}}
        ]
      }
    ]
  },
  "exit": {
    "on_false": true,
    "stop":   {"kind": "percent", "percent": 5.0,
               "atr_period": 14, "atr_mult": 2.0},          // percent | atr | none
    "target": {"kind": "r_multiple", "r": 2.0, "percent": 10.0},  // r_multiple | percent | none
    "max_hold_bars": 60                                     // optional safety time stop
  }
}
```

- **Groups** are the "blocks". A block = one group of conditions; blocks are joined by the
  group's single connector chip (AND/OR). Mixed operators at one level are expressed with a
  **nested group** (`+ Add group`) — a visual chip per edge would be ambiguous, so one chip
  per group is deliberate.
- **Operands**: `source: "indicator"` resolves to a series from the catalog; `source: "value"`
  is a number. `offset` shifts any series N bars back (RSI now vs RSI 1 bar ago).
- `right.source: "indicator"` covers the chosen "close crosses above SMA(50)" case; the UI
  defaults `right` to a plain number so the common path stays a number box.
- `name` is owned by the row, not the payload: `save_rule(name, payload)` sets
  `payload["name"] = name` before validation and storage, so the column, the JSONB document
  and the paper picker cannot disagree.
- **Limits** (server-enforced): depth ≤ 4, ≤ 12 blocks, ≤ 40 conditions, name 1-80 chars,
  unique name.

### 2.2 Indicator catalog (`analysis/rule_catalog.py`)

One declarative table drives both the UI picker and validation:

```python
Indicator(id="rsi", label="RSI (Wilder)", group="Momentum",
          params=(Param("period", "Period", default=14),),
          outputs=("value",), compute=_rsi,
          warmup=lambda p: int(p["period"]) + 1)
```

Minimum set (all computed from OHLCV, no TradingView session required):

| id | label | params | notes |
|---|---|---|---|
| `open` `high` `low` `close` `volume` | price series | — | `volume` also usable as ratio source |
| `sma` `ema` `wma` | moving averages | `period` | EMA is new (`close.ewm`) |
| `rsi` | RSI (Wilder) | `period` | reuse `rsi_wilder` |
| `macd` | MACD | `fast`, `slow`, `signal` | outputs `line`, `signal`, `hist` |
| `stoch` | Stochastic | `k_period`, `d_period` | outputs `k`, `d` |
| `atr` | ATR | `period` | reuse `IndicatorEngine.atr` |
| `bb` | Bollinger | `period`, `std_dev` | outputs `upper`, `mid`, `lower` |
| `obv` | OBV | — | |
| `vwap` | Rolling VWAP | `period` | **not** `IndicatorEngine.vwap()` — that is cumulative over the whole loaded frame (≈450 daily bars), which is meaningless as a daily rule. Compute `rolling(period)` sums of typical×volume / volume. |
| `rvol` | Relative volume | `period` | volume / SMA(volume, period) |
| `pct_change` | % change | `bars` | |
| `highest` / `lowest` | N-bar extreme | `period` | breakout conditions |
| `adx` | ADX | `period` | **new**; the trend-vs-chop measure the chat asked for |
| `atr_pct` | ATR % of price | `period` | volatility regime |
| `dist_sma_atr` | Distance from SMA in ATR | `period`, `atr_period` | trend strength |

Unknown id / out-of-range param → validation error naming the field.

Implementation notes confirmed against the repo: `IndicatorEngine.atr()` is **EMA-smoothed**
(`ewm(span=period)`), not Wilder, so `adx` must use Wilder smoothing (a local RMA helper, or
`indicators/_math.rma` if that import stays cycle-free) — otherwise the trendiness reading is
biased. ATR-based stops keep `IndicatorEngine.atr` for consistency with the rest of the engine.
Bollinger keeps the engine's `rolling().std()` (sample std, `ddof=1`), matching the existing
pattern code rather than TradingView's population std.

Each catalog entry also declares a `warmup(params)` value used by `required_bars()`. Do not
infer warmup from the largest parameter: MACD needs `slow + signal`, ADX needs ≈`2 × period`,
and an operand `offset` adds to it.

### 2.3 Persistence — new `rules` schema

`migrations/rules/0001_rules.sql` (DDL only; runner owns schema/search_path):

```sql
CREATE TABLE indicator_rules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    generation  BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    payload     JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'
                                      AND payload->>'schema_version' = '1'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX indicator_rules_name ON indicator_rules (lower(name));
CREATE INDEX indicator_rules_updated ON indicator_rules (updated_at DESC);
```

Copy the quartet from `migrations/kronos/` + `core/kronos_db.py` + `scripts/setup_kronos_db.py`:
`core/rules_db.py` (`schema_name` regex `rules(?:_[a-z0-9_]+)?`, `configured_dsn`, `connection`,
`migration_files`, `verify_migrations`, `migrate`), `core/rules_store.py`
(`list_rules`/`get_rule`/`save_rule`/`delete_rule`/`ping`, `Jsonb(...)`, `FOR UPDATE` +
`expected_generation` conflict, `updated_at=clock_timestamp()`, `open_rules_store()`).

- `core/rules_db.py` defines `RulesError(ValueError)`, `RulesDatabaseUnavailable`,
  `RulesMigrationRequired`, and `RuleConflict` (duplicate `lower(name)`, stale generation,
  or a running book). The web layer maps those; the store never leaks a DSN.
- `delete_rule` must refuse with `RuleConflict` while a book uses it. Add a
  `rule_in_use(rule_id)` helper modelled on `data/tv_stacks.py:155-162` (`stack_in_use`):
  acquire each book's lock and return True only for a **running** book whose `rule_id` or
  `rule_filter_id` matches (and whose filter mode is not `off`). Stopped books clear those
  fields in `finally`, so a stale id cannot block deletion. The delete refusal names the
  market, e.g. "Stop the US paper book using this indicator before deleting it".

Config additions (`config.py`, `.env.example`):
`rules_database_url: str = ""`, `rules_schema: str = "rules"`.
`scripts/setup_rules_db.py` wired into `install.sh` and `deploy_to_contabo.sh` non-fatally,
same as kronos.

### 2.4 Contracts (`core/rule_contracts.py`)

Pydantic models (`RulePayload`, `Group`, `Condition`, `Operand`, `ExitPlan`) validate
structure. `analysis.rule_catalog.validate_rule()` then checks indicator ids, parameter ranges
and output fields; the store, the HTTP routes and the detector all call both, so a hand-crafted
JSON body cannot bypass the editor.

---

## 3. Evaluation engine (`analysis/rule_engine.py`)

```python
def evaluate_series(rule: RulePayload, df: pd.DataFrame) -> pd.Series: ...   # bool per bar
def evaluate_bar(rule, df, index=-1, series: pd.Series | None = None) -> dict:
    """{"matched": bool, "warm": bool,
        "conditions": [{"label": "RSI(14) >= 30", "left": 42.1, "right": 30.0, "pass": True}, ...]}"""
def required_bars(rule) -> int
```

- Resolve each **distinct** `(id, params, field)` series once per call (dict cache keyed by
  that tuple) — a rule with 10 conditions on RSI(14) computes RSI once. The validated
  `RulePayload` is reused on the detector/filter instance; the remaining tree walk is O(nodes).
- `crosses_above`: `prev_left <= prev_right and left > right`; `crosses_below` mirrored.
  The detector needs the last **two** bars of one `evaluate_series` result, not two separate
  evaluations (two calls would recompute every series twice per symbol/bar).
  `analyze()` therefore does `series = evaluate_series(...)`, fires only when
  `series.iloc[-1] and not series.iloc[-2]`, then calls `evaluate_bar(..., series=series)` once
  for the log/evidence payload.
- `eq`/`ne` use a relative tolerance (`np.isclose(rtol=1e-9, atol=1e-9)`) — raw float equality
  is a footgun on indicator values.
- `NaN` anywhere in a condition ⇒ that condition is `False`; `required_bars` =
  `max(period + offset for every operand) + 2`, floored at 60 so ATR/ADX warm up.
- `evaluate_series` is vectorized (pandas) for backtests; `evaluate_bar` is the live path and
  returns per-condition evidence for logs and the UI.
- **Index alignment matters**: `IndicatorEngine.__init__` calls
  `df.copy().reset_index(drop=True)`, so its outputs carry a `RangeIndex`, while
  `OHLCVStore.get_df()` returns a normalized `DatetimeIndex`. Compute every operand on one
  `reset_index(drop=True)` frame (or on the engine's own series) and reindex the final boolean
  series back to `df.index`. Mixing the two silently produces all-`NaN` comparisons.

## 4. Detector adapter (`analysis/rule_detector.py`)

`VisualRuleIndicator(BaseIndicator)`, modelled line-for-line on `TvStackIndicator`:

```python
class VisualRuleIndicator(BaseIndicator):
    def __init__(self, rule: dict):        # same dict the store returns
        self._rule = RulePayload.model_validate(rule["payload"] | {"name": rule["name"]})
        # `_trail_indicators` only calls trail() when this is True. Leaving it
        # False while implementing trail() would silently disable exits.
        self.trails = bool(self._rule.exit.on_false)
    @property
    def name(self): return self._rule.name
    @property
    def timeframes(self): return [self._rule.timeframe]
    @property
    def MIN_BARS(self): return required_bars(self._rule)
    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or len(df) < self.MIN_BARS: return None
        series = evaluate_series(self._rule, df)          # bool dtype, NaN already False
        now = bool(series.iloc[-1])
        prev = bool(series.iloc[-2]) if not pd.isna(series.iloc[-2]) else False
        if not now or prev: return None                   # edge only
        evidence = evaluate_bar(self._rule, df, -1, series=series)
        action = "BUY" if self._rule.direction == "long" else "SELL"
        stop, target = exit_levels(self._rule.exit, self._rule.direction, df)
        sig = emit(self, df, snapshot, action=action, stop=stop, target=target,
                   notes=f"Rule {self.name} matched: " + "; ".join(...))
        if sig: sig.provenance = "visual_rule"; sig.rule_evidence = evidence
        return sig
    def trail(self, df):
        if not self._rule.exit.on_false: return None
        series = evaluate_series(self._rule, df)
        stop, _target = exit_levels(self._rule.exit, self._rule.direction, _last(df, "close"), df)
        return (self.action, stop) if series.iloc[-1] and stop is not None else None
```

- `emit()` **requires a finite stop**. `exit.stop.kind == "none"` therefore means "no explicit
  stop in the UI" and `_exit_levels()` supplies a default 2×ATR(14) protective stop; the exit
  plan's `none` is reserved for the target only. Reject a rule where both stop and target are
  `none` at validation, so a strategy always has a defined exit.
- `_exit_levels()` must return a bracket on the correct side of the last close (long:
  `stop < price < target`; short: `target < price < stop`). Return `None` to skip the signal
  rather than passing a wrong-side level — `emit()` rejects that anyway, but the log would
  say "no signal" instead of naming the broken exit plan.
- `emit()` supplies the chart markers and stop/target validation; no new signal contract.
- `VisualRuleIndicator.__init__` must not touch the DB; the scanner loads the rule dict before
  constructing it (same as `TvStackIndicator`).
- **Do not put this class in `indicators/`**: `indicators.discover()` instantiates every
  `BaseIndicator` subclass with **no arguments** (`indicators/__init__.py:11-25`), and this one
  requires a rule dict. Keep it in `analysis/rule_detector.py` and inject it explicitly from
  `_discover_patterns`, exactly like `TvStackIndicator`.
- Add `rule_evidence: dict | None = None` to `TradeSignal` (`patterns/base_pattern.py:158`,
  next to `laya_assessment`) so the fill trace keeps the condition values.
  **Persistence needs three more edits**, otherwise the evidence is dropped:
  1. `BacktestTrade` (`core/backtester.py:144`, beside `laya_assessment`) gains
     `rule_evidence: dict | None = None`.
  2. `_open_trade` (`core/backtester.py:909-910`) copies
     `rule_evidence=dict(signal.rule_evidence) if getattr(signal, "rule_evidence", None) else None`.
  3. `core/pattern_provenance.payload()` and `row_payload()` (`:24-34`) add `'rule_evidence'`
     to their key tuples — `_append_signal_log` and the trade export read only those keys.

## 5. Integration A — strategy (paper book)

Mirror `tv_stack_id` at every hop:

| Layer | Change |
|---|---|
| `web/app.py` `PaperStartRequest` | `rule_id: str = ""`, `rule_filter_id: str = ""`, `rule_filter_mode: Literal["off","shadow","filter"] = "off"` |
| `web/app.py:_start_book` | `rule_mode = bool(rule_id)`; `indicators_only = payload.indicators_only or bool(tv_stack_id) or rule_mode`; pass `rule_id=`, `rule_filter_id=`, `rule_filter_mode=`. Validate that a filter id is not the same as the strategy rule id. |
| `core/paper_books.py:PaperBook.start` | accept `rule_id`, `rule_filter_id`, `rule_filter_mode`; when `rule_id` is set force `indicators_only=True; pattern_only=False; patterns_v2=False` (inside `PaperBook.start`) and validate the rule exists. Follow the existing convention: return a refusal **string** (e.g. `"Unknown indicator rule."`) instead of raising. |
| `core/paper_books.py:_thread_main` / `_run_thread` | `start()` passes its worker arguments **positionally** (`core/paper_books.py:start`); add the three rule kwargs to that tuple and to `_run_thread`'s signature, or the thread starts with shifted arguments. Pass `rule_id=`, `rule_filter_id=`, `rule_filter_mode=` into `MarketScanner` inside `_run_thread`. |
| `core/paper_books.py` lifecycle | set `self.rule_id` / `self.rule_filter_id` on start and **clear them in the `finally` block** (`core/paper_books.py:630`), otherwise `rule_in_use()` blocks deletion forever after a stopped book. Add the rule name to the running status string. |
| `core/scanner.py:MarketScanner` | `rule_id: str = ""`, `rule_filter_id: str = ""`, `rule_filter_mode: str = "off"`; `_indicators_only = ... or bool(rule_id)` |
| `core/scanner.py:_discover_patterns` | new first branch: `if self._rule_id: self._patterns = [VisualRuleIndicator(get_rule(self._rule_id))]` |
| `web/templates/paper.html` + `web/static/app.js` | per-book "Rule" select (`/api/rules`) and "Rule filter: off/shadow/filter" select; include them in `bookStartBody()` (`app.js:585`), which is the actual payload builder. |
| `web/templates/builder.html` | "Start paper book" panel (market, symbols, rule) → `POST /api/paper/start`, copied from `indicators.html:66-83` |

`_trail_indicators` (`core/scanner.py:1019`) already handles `trails`/`trail()` for any
`BaseIndicator`, so exits on rule-false need no scanner change **provided the detector sets
`trails=True` when `exit.on_false` is set**. The loop skips `trails=False` entirely and treats
`trail()` returning `None` as "exit now". Note also that `indicators_only` books allow only one
open position per symbol (`core/paper_trader.py:723-735`) — a rule strategy inherits that.

## 6. Integration B — filter (context gate)

- Insert into `_finish_signal` **after** the volume gate (`core/scanner.py:1402`) and before
  vision/Laya. Apply it only when the signal's timeframe matches the rule's timeframe; a 1d
  rule must not be evaluated against 1W bars:

```python
if (self._rule_filter is not None
        and signal.provenance != "visual_rule"
        and signal.timeframe == self._rule_filter.timeframe):
    df = self._store.get_df(...)
    if df is None:
        ev = {"matched": False, "warm": False,
              "conditions": [], "reason": "insufficient history for the rule"}
    else:
        key = (signal.symbol, signal.timeframe, df.index[-1])   # one eval per bar
        ev = self._rule_filter_cache.get(key) or evaluate_bar(self._rule_filter, df, -1)
        self._rule_filter_cache[key] = ev
    if self._rule_filter_mode == "shadow":
        signal.rule_evidence = ev                      # record only, never blocks
        self.stats["rule_filter_shadow"] = self.stats.get("rule_filter_shadow", 0) + 1
    elif not ev["matched"]:
        failed = [f"{c['label']} (got {c.get('left')})"
                  for c in ev["conditions"] if not c["pass"]]
        failed = failed or [ev.get("reason") or "rule not satisfied"]
        self._append_signal_log(signal, status="rejected",
            reason="Rule filter: " + "; ".join(failed))
        self.stats["signals_rejected"] += 1
        self.stats["rule_filter_rejected"] = self.stats.get("rule_filter_rejected", 0) + 1
        return
```

- **Fail closed** when the rule cannot be evaluated (short history, missing indicator):
  `filter` mode rejects with the reason above; `shadow` records it. Trading without the
  context the operator asked for is the bug being fixed.
- Cache the per-bar evaluation: several pattern families can fire on the same symbol/bar, and
  each would otherwise recompute every series. Initialize `_rule_filter` (loaded once via
  `get_rule`, not per signal) and `_rule_filter_cache` in `MarketScanner.__init__`, and clear
  the cache when a new bar is observed (`_apply_bar`) so it cannot grow without bound.
- Applies to every detector family (patterns, patterns_v2, local indicators) — that is the
  point: it supplies the before-the-pattern context the current pipeline lacks
  (`core/scanner.py:51-101` regime hooks are no-ops).
- Rejection reasons land in the existing Paper → Logs tab and in `logs/paper_signals_*.jsonl`,
  so failed filters are inspectable, not silent.

## 7. Integration C — rule backtest (`core/rule_backtest.py`)

Deliberately **separate** from `core/backtester.py` (pinned-version coupling). A compact,
deterministic walk:

1. Load bars with the existing loaders (`core/backtester.py:_load_cached_ohlcv` /
   `fetch_ohlcv_candles`). `_load_cached_ohlcv` is module-private; either import it deliberately
   or factor the two-line call into a small shared helper — do not duplicate the cache format.
2. Warmup = `required_bars(rule)`; evaluate the tree per bar with `evaluate_series`.
3. Entry on the **next bar's open** after a false→true edge (same semantics as paper fills,
   `core/execution.py:43-80`), with `slippage_pct` and `txn_cost_pct`.
4. Exit on: rule false at close (next open), stop touched intrabar, target touched intrabar,
   or `max_hold` bars. Stop/target from `exit.stop` / `exit.target`.
5. Return `{"trades": [...], "summary": {trades, win_rate, avg_pnl_pct, expectancy,
   max_drawdown, max_dd}, "matches": [{"date", "close", "matched"}]}` — raw rows, so the UI
   can reconcile numbers. Both `max_drawdown` and the plan's `max_dd` alias are present.

Runtime budget: one pass over ≤ 450 daily bars per symbol, vectorized series computation —
sub-second per symbol on this box; cap a run at 200 symbols and run it in
`asyncio.to_thread` like the existing preview route.

## 8. Web surface

### 8.1 `web/rules.py` — `register_rules_routes(app, templates, ctx)`

| Method + path | Purpose |
|---|---|
| `GET /builder` | page, `active="builder"` |
| `GET /api/rules` | `{"rules": [{id, name, generation, updated_at, summary}]}` — no full payloads |
| `GET /api/rules/{id}` | full rule |
| `POST /api/rules` | `{name, payload, rule_id?, expected_generation?}` → 201/409 on stale generation |
| `DELETE /api/rules/{id}` | `{"ok": true}`; 409 if a running paper book uses it |
| `GET /api/rules/catalog` | indicator/operator catalog for the picker |
| `POST /api/rules/validate` | validate a draft tree; per-node `{ok:false, errors:[{loc,msg}]}` |
| `POST /api/rules/preview` | `{rule_id | payload, symbol, market, bars}` → per-bar table + condition evidence |
| `POST /api/rules/{id}/backtest` | run `rule_backtest`, return trades + summary + matches |

Register the **static paths before the `{rule_id}` path** (`/api/rules/catalog`,
`/api/rules/validate`, `/api/rules/preview` before `/api/rules/{rule_id}`), or FastAPI matches
`rule_id="catalog"`.

Bound the preview: `bars` defaults to 120 and is capped at 500, one symbol per request. The
backtest endpoint caps at 200 symbols (section 7). Both execute inside `asyncio.to_thread`.

Errors: copy the Tasks contract (`web/tasks.py:55-72`, `_failure` → `_err`), returning
`{"error_code": "...", "detail": "..."}` with 400 invalid input, 409 `RuleConflict`,
503 `RulesDatabaseUnavailable` / `RulesMigrationRequired`, 500 unexpected. The global `api()`
helper reads `detail`, so it surfaces the message unchanged. All DB work inside
`await asyncio.to_thread(...)`. Auth: `Depends(require_login)`; page route follows
`web/app.py:361-363` and, like `tasks_page` (`web/tasks.py:84-97`), pings the store and renders
a readable setup hint (`scripts/setup_rules_db.py`) instead of failing the whole app.

### 8.2 `web/templates/builder.html` + `web/static/builder.js`

Saved + Editor sit side by side; Preview, Backtest and Start paper book are
full-width panels below. All existing CSS primitives are used (`.panel`,
`.toolbar`, `.btn`, `.table-wrap`, `.status`):

```
┌ Saved ────────────┐┌ Editor ──────────────────────────────────┐
│ [select] [New]    ││ indicator1        [Save] [Delete]        │
│ [Duplicate]       ││ Timeframe [1d]  Direction [Long]        │
│                   ││ ┌ Group (AND) ──────────────────────┐   │
│                   ││ │ ⠿ Block 1                         │   │
│                   ││ │   RSI(14) [>=] [30]        [+] [x]  │   │
│                   ││ │   close [crosses above] SMA(50)    │   │
│                   ││ │            ┌────────┐              │   │
│                   ││ │            │ AND ▾  │  ← chip      │   │
│                   ││ │ ⠿ Block 2                         │   │
│                   ││ │   ADX(14) [>=] [20]                │   │
│                   ││ └────────────────────────────────────┘   │
│                   ││ [+ Add block] [+ Add condition]           │
│                   ││ Exit: [on rule false] Stop [percent 5%]  │
│                   ││       Target [2R]                        │
└───────────────────┘└──────────────────────────────────────────┘
┌ Preview / Backtest ──────────────────────────────────────────┐
│ Symbol [AAPL] Market [US] [Preview]  ← matches for last 120 bars
│ table: date | close | matched | per-condition evidence
│ Backtest: [symbols] [start] [end] [Run] → trades + summary + match timeline
└──────────────────────────────────────────────────────────────┘
```

`builder.js` (no framework, follows `indicators.js` / `tasks.js` conventions):

- `state = {rule, savedId, generation, dirty}`; `renderEditor()` rebuilds the editor DOM from
  state.
- **Drag/drop**: `Sortable.create(container, {group: "rule-blocks", handle: ".builder-handle",
  draggable: ".builder-condition, .builder-group", ghostClass: "builder-ghost", onEnd})` per
  group container; `onEnd` moves the child object between parents in `state.rule.root` by
  `data-path` (a dotted path such as `root.2.0`, where each segment indexes the parent's
  condition/group children) and re-renders. Connector chips are **not** draggable, and
  `newDraggableIndex` (not `newIndex`) is used so chips never shift insertion indexes. Same
  library as Kanban, no new vendor file.
- **Fallback**: ▲/▼ buttons per block and per condition (keyboard + touch), so drag is not the
  only way to reorder.
- **Condition row**: indicator `<select>` (from catalog, grouped Price / Trend / Momentum /
  Volatility / Volume), a params chip (`(14)` / `(12, 26, 9)` → click opens the number inputs),
  operator `<select>`, and an operand control that is a number input by default with a
  "vs indicator" toggle.
- **Connector chip**: one per group, click toggles AND↔OR, label shows the current operator.
- **Dirty guard**: `beforeunload` + confirm on switching rules.
- **Safety**: rule names, indicator labels and condition text go into the DOM with
  `textContent` (never `innerHTML`); ids/operators/params are validated server-side against
  the catalog, so a crafted payload cannot inject a query, an import, or HTML.
- **Preview**: renders the per-bar table (mandatory, so a match can be verified) and a small
  match-count summary; optionally drops markers on the existing `tv_chart.js` chart — do this
  only after the table works.

CSS: append a `.builder-*` section to `web/static/app.css` (single-stylesheet convention);
bump `?v=` in `base.html:7` and in the `builder.js` script tag of `builder.html`.

Nav: one line in `base.html` after Indicators —
`<a href="/builder" class="{% if active == 'builder' %}active{% endif %}">Builder</a>`.

## 9. Files touched

**New**
```
migrations/rules/0001_rules.sql
core/rules_db.py
core/rules_store.py
core/rule_contracts.py
core/rule_backtest.py
analysis/rule_catalog.py
analysis/rule_engine.py
analysis/rule_detector.py
scripts/setup_rules_db.py
web/rules.py
web/templates/builder.html
web/static/builder.js
analysis/test_rule_engine.py
analysis/test_rule_detector.py
core/test_rules_store.py
core/test_rule_backtest.py
web/test_rules_api.py
```

**Modified**
```
config.py                     rules_database_url, rules_schema
.env.example                  both, plus RULES_TEST_DATABASE_URL note
web/app.py                    PaperStartRequest fields, _start_book, register_rules_routes
core/paper_books.py           start(...), _thread_main args, _run_thread, lifecycle fields
core/scanner.py               rule_id branch, filter gate + cache, stats
core/backtester.py            BacktestTrade.rule_evidence + _open_trade copies it
core/pattern_provenance.py    payload()/row_payload() include rule_evidence
patterns/base_pattern.py      TradeSignal.rule_evidence
web/templates/base.html       nav link
web/templates/paper.html      rule + rule-filter selects
web/static/app.js             paper start payload
web/static/app.css            .builder-* styles (+ v bump)
conftest.py                   rules_store_factory
install.sh, deploy_to_contabo.sh   setup_rules_db.py
README.md                     full Builder section: how it works, setup, config table,
                              endpoints, examples, safety notes, troubleshooting
docs/visual-indicator-editor.md    this design document
```

Out of scope on purpose: the Tk desktop UI (`ui/`), the main `Backtester`/`BacktestService`
path, and TradingView stack plots as rule operands (rules are self-contained local math).

## 10. Verification

**Setup**
1. `python scripts/setup_rules_db.py` → `migrations: applied 1`.
2. `psql -c "\dt rules.*"` → `indicator_rules` + `schema_migrations`.

**Automated**
```
pytest analysis/test_rule_engine.py core/test_rules_store.py core/test_rule_backtest.py \
       analysis/test_rule_detector.py web/test_rules_api.py -q
```
DB coverage follows the two existing conventions: `core/test_rules_store.py` uses a
`rules_store_factory` pytest fixture in `conftest.py` (skip without `RULES_TEST_DATABASE_URL`,
`migrate()` into `rules_test_<uuid>`, monkeypatch settings, `DROP SCHEMA ... CASCADE`);
`web/test_rules_api.py` is a `unittest.TestCase` with
`DSN = os.environ.get("RULES_TEST_DATABASE_URL")` and
`needs_db = unittest.skipUnless(DSN, ...)`, exactly like `web/test_tasks_api.py:14-41`.
The engine/detector tests are pure pandas and need no database.

**Editor, end to end**
1. `/builder` → New → name `indicator1` → Add block → RSI(14) `>=` 30 → add `close crosses above SMA(50)`.
2. Add a second block → ADX(14) `>=` 20 → toggle the chip to `OR` → drag block 2 above block 1 → Save.
3. Reload the page and reopen the rule: tree, operators, params and order must be identical.
4. Drag a condition into a nested group; Save → reload → structure preserved.

**Preview (verify, don't trust)**
5. Preview `AAPL` (US) for 120 bars → table shows per-bar close, each condition value, pass
   flag, and final match; match count equals the number of rows where `rule` is true.
6. Flip `>=` to `<=` and confirm both the match count and the row flags change accordingly.

**Strategy**
7. Builder → Start paper book (US, 5 symbols, `indicator1`) → status shows Indicators-only=ON.
8. Signal log fills on the next scan; `logs/paper_signals_us.jsonl` shows
   `provenance: visual_rule` and each fill's `rule_evidence`. Restart the app and confirm the
   same evidence is still on the position/trade export (it is copied into `BacktestTrade` by
   `_open_trade`, not only held on the signal).
9. Let a position run until the rule turns false → exit reason is the indicator trail, not a
   stop.

**Filter (the original complaint)**
10. Start a patterns_v2 paper book with Rule filter = `indicator1`, mode = `filter`.
11. Confirm Scan Log shows `Rule filter: ADX(14) >= 20 (got 12.4)` rejections on sideways
    breakouts, and that the same signals pass when the rule is removed.
12. Switch mode to `shadow`: signals still fill, but each logged signal carries
    `rule_evidence` — a measured comparison, no behaviour change.

**Backtest**
13. Run `indicator1` over 2 symbols, 1 year → trades table, summary, and match timeline;
    recompute one trade's P&L by hand from the raw rows and confirm it matches.

**Failure paths**
14. Save with a duplicate name → 409 with a clear message; delete a rule used by a running
    book → 409 naming the book; stop it, then delete → `{"ok": true}` and the row is gone.

## 11. Audit notes (2026-10-21)

Verified directly against the source, not just the earlier exploration summaries:

- **Stack facts confirmed**: FastAPI + Jinja2 (`web/app.py:230-256`), vendor-free vanilla JS,
  SortableJS already at `web/static/vendor/sortable.min.js` (used at `tasks.js:472-490`),
  `TvStackIndicator` as the JSON-detector precedent (`data/tv_stacks.py:379-471`), the
  `MarketScanner`/`BaseIndicator`/`emit()` contract, and the `*_db.py` + `migrations/<schema>/`
  + `scripts/setup_*_db.py` quartet (`install.sh:144`, `deploy_to_contabo.sh:108`).
- **Paper plumbing confirmed**: `_start_book` (`web/app.py:1117`), `PaperBook.start`
  (`core/paper_books.py:502`), `_run_thread` scanner construction (`:803`),
  `_discover_patterns` (`core/scanner.py:1688`), `_finish_signal` volume gate
  (`:1381`), and `_trail_indicators` (`:1019`).
- **Dedupe worry resolved**: `stable_anchors` assigns a unique microsecond anchor from the
  signal bar when `setup_key` is absent (`core/execution.py:10-20`), so an edge-triggered rule
  can re-enter on a later crossing without colliding with `consumed_anchors`.
- **Corrections applied to this document**: added the missing `rule_evidence` persistence path
  (`BacktestTrade` + `_open_trade` + `pattern_provenance.payload`), the `trails=True`
  requirement (otherwise `_trail_indicators` never calls `trail()`), the `_thread_main`
  positional-argument plumbing and lifecycle clearing, Wilder ADX, the instance-level
  `evaluate_series` fix (one pass, not two `evaluate_bar` calls), fail-closed filter behaviour,
  route ordering for `/api/rules/catalog`, the `error_code`/`detail` contract, and the actual
  `bookStartBody` line (`app.js:585`).
- **Implementation checks (2026-10-22)**: `scripts/setup_rules_db.py` applied migration 0001
  and was idempotent on a second run; `rules.indicator_rules` + `schema_migrations` present.
  The new suite passed **30 tests** with a disposable PostgreSQL database
  (`RULES_TEST_DATABASE_URL`). The full non-slow suite: **779 passed, 1 failed** —
  `data/test_tv_stacks.py::test_fake_series_buys_then_trails_out`, re-run in a clean `HEAD`
  worktree and failing there too (pre-existing stale test against the committed `causal-v1`
  paper model), not caused by this feature.
- **Browser checks (Playwright/Chromium)**: logged in, opened `/builder`, confirmed 22 catalog
  indicators, created `indicator1`, added two blocks and a second condition, toggled a connector
  AND→OR, dragged a block to the top, edited the RSI params chip (14→21), dragged the root
  condition into a nested block, saved, reloaded and reopened the rule (structure and `(21)`
  preserved), previewed AAPL (120 rows, per-condition evidence, 4,389 bars available), and
  deleted it. No page or console errors. The Paper tab loaded both rule selects, selecting a
  strategy rule auto-checked `Indicators only` and disabled `Pattern-only`, and the filter mode
  selected `shadow`.
- **Additional fixes from this audit**: catalog ids/params/output fields are now validated at
  save, validate and detector construction (`analysis.rule_catalog.validate_rule`); filter ids
  in `off` mode no longer block deletion; delete refusals name the market; a strategy/filter
  pair with the same id is refused in `_start_book` as well as `PaperBook.start`.
- **Deliberate deviation**: chart markers in Preview remain the optional stretch item from
  section 8.2. The main `Backtester`/`BacktestService` path stays untouched by design.

