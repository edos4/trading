# Historical Experiments

Open **Experiments**, select US or PH, supply inclusive start/end session dates
and click **Start all folders**. A batch creates a separate result record for
each of the 449 archived writeups. Search or filter folders, then click one to
view positions, closed trades, signals, performance, event outcomes, logs and
its source/integration plan. Click a symbol to open the same chart viewer used
by Paper, using this batch's frozen data and saved record levels. Complete
folder evidence is downloadable as JSON Lines once the folder finishes;
failed/cancelled/interrupted folders export their explicitly labeled retained
partial results.

## Source fidelity and current coverage

The user-approved policy is to block incomplete strategies and unavailable or
proprietary inputs, while permitting source-defined indicator/event studies.
Readiness is an explicit reviewed registry in `core/experiments/adapters.py`;
Markdown is never executed or converted into guessed trading rules.

| Folder | Implemented scope |
| --- | --- |
| 040 | Visible SPY daily example: prior-20-session high breakout, prior-10-session low or 2.5 Wilder ATR trail exit, next-open fills, 0.5% equity risk, finite cash, long-only. US only. |
| 406 | Separate 10/50, 8/21, 20/50 and literal 10/50-cross **OR** price-above-100-EMA panels. Cross-down exits, next-open fills, independent fixed-notional positions. |
| 048 | Completed three-candle outer-wick FVG geometry. Research book: next-open fill in the gap's direction, fixed notional, exit after 20 sessions. Source stop, retest and invalidation rules remain unresolved. |
| 056, 145 | Rolling Z-score 50/EMA3 extremes and trend-context, traded as a research book. Momentum-column confirmation and divergence remain blocked. |
| 353 | Fresh 20>50>100 EMA alignment, traded long as a research book. Pullback entries remain blocked. |
| 368 | 50/200 SMA crosses and prior-20-session Donchian breaks, each a research book. Opinion grades remain blocked. |
| 385, 433 | Candle body/wick/range measurements. Research side is a candle-color change. No source order is inferred. |
| 004 | EMA34 high/close/low channel. Research side is a fresh close-EMA slope sign. Profile and sector branches remain blocked. |
| 025 | EMA9/20/50 measurements. Research side is an EMA9/20 cross. Session VWAP and pullback trading remain blocked. |
| 110 | Prior-day high/low/midpoint. Research side is a fresh close through the prior high or low. The intraday model remains blocked. |
| 115 | Candle-anatomy component. Research side is a candle-color change. The engulfing trade model remains blocked. |
| 120, 167 | Stochastic 5/3/3. Research side is a K/D cross. These repeat one tutorial. Trendline trading remains blocked. |
| 184 | The writeup's additional three-candle FVG definition, traded like 048 and distinguished from the video's qualitative account. |
| 258, 311 | DEMA200 measurements. Research side is a close/DEMA cross. Supertrend and order rules remain blocked. |
| 394 | MACD12/26/9. Research side is a MACD/signal cross. Session VWAP and the combined trade policy remain blocked. |
| 400 | SMA9. Research side is a close/SMA cross. Reversal, session and volume rules remain blocked. |
| 414, 431 | Bullish EMA9/21 or bearish EMA50/200 crosses. The source says these are not entries; the research book trades them anyway. |

This is **22 runnable folder adapters** on US, with **427 source-specific
blockers**. PH has 21 runnable adapters; the SPY-specific example is blocked
there. Twenty adapters are research books: the source never froze an order, so
the run uses one fixed-notional position, a next-open fill, and a 20-session
exit. That book is **not** a completed implementation of the source's
underspecified trading strategy.
The UI exposes each adapter's exact scope, parameters, operational assumptions
and remaining limitations before a run. Unknown/default-only formulas,
intraday/tick sequences, profiles/footprints, options, external positioning
feeds, repainting outputs and non-trading sources stay blocked as applicable.

## Execution and data

Daily data always comes through `data/history.py`, as described in
[daily-bars-api.md](daily-bars-api.md): the history owner reads local PostgreSQL;
other processes use the existing authenticated HTTP client. There is no CSV,
Yahoo or TradingView fallback and no ingestion/repair. Symbol enumeration is a
current market snapshot; historical membership/delistings and provider
corporate-action adjustments are unverified. PH storage keys retain `.PS`.

Each symbol is loaded once per batch without a newest-N cap, validated, cut at
the selected end session/last completed session, then shared among eligible
folder adapters. Earlier bars warm indicators only. A same-timestamp
conflict, a non-finite value, a transport failure, or a symbol with no
usable bar fails that symbol and is reported. A folder that still evaluated
other symbols stays complete, with those symbol failures counted in
coverage. An invalid print drops that
session only. Identical copies collapse. When a 09:30 daily bar shares a
session with a local-midnight or other extra print, the 09:30 bar is kept.
An empty enumeration is treated as an unavailable/empty-universe error because
the existing facade folds enumeration failures into an empty list.

Close signals fill at the next observed open within the requested range.
The last signal is unfilled if no later eligible bar exists. Positions can
remain marked open at the end or be liquidated at the last observed close,
with an explicit liquidation reason. Fees and slippage apply to actual fills;
open-position marks include entry fees, without claiming an unobserved closing
fill. PH quantities use historical board-lot dates. Cross-only EMA strategies
do not receive invented stops or targets. Their independent fixed-notional
accounting does not model finite portfolio cash or exposure; the SPY example
uses its own finite-cash risk model. Variant capital/results are never pooled.

Research books report trade P&L for that 20-session hold, including fees and
slippage. A new signal while a position is open is skipped. PH does not open
shorts. The 5/20 forward-return study is no longer emitted for these folders.
Outcome values never enter signal generation.

## Storage and lifecycle

`EXPERIMENTS_STORE_DIR` defaults to `logs/experiments`, resolved against the
repository root. This independent SQLite run store contains compressed source
snapshots, requests, validated OHLCV snapshots/digests, per-folder records and
paginated evidence. SQLite stores experiment artifacts; it does **not** replace
PostgreSQL as the history provider. Live Paper books, the pattern registry and
history data are not written by the experiment service.

One batch runs at a time across processes sharing this store. A dedicated
heartbeat renews its lease; expired work is marked interrupted, retaining
partial evidence. Cancellation is checked between symbols/adapters and inside
replays. An explicit new Start retries with a fresh request key; automatic
replays against changed data do not occur. Idempotency keys deduplicate an
identical submission and reject reuse with different inputs. Browsers reconnect
to saved runs by ID. Storage and batch coordination are local to one web host;
multi-host run coordination is not implemented.

`core/experiments/sources.json` bundles all writeups/plans because
`indicator-vides/` is gitignored. Rebuild it after source edits:

```bash
.venv/bin/python scripts/build_experiment_catalog.py
```

Existing run manifests retain their original source text/hashes. Engine and
dataset hashes, costs, market, date range and universe are recorded with each
batch. No schema setup or new dependency is required. Deploy/restart the normal
web application to expose the new route; no systemd/cron files are changed.
`reviewed_sources.json` pins each runnable adapter to its reviewed writeup and
plan revisions. Rebuilding a changed source bundle blocks that adapter until
its implementation, tests and pins are reviewed together; it cannot silently
reinterpret an existing strategy.

## Adding a source adapter

Resolve that folder's gaps first. Register its reviewed definition and causal
feature/order logic in `adapters.py`, with exact scope, parameters, warm-up,
assumptions, market/universe eligibility and remaining limitations. Add numeric
fixtures and prefix-invariance tests. Stateful sequences must retain the
availability time of pivots/zones, and synthetic prices must not become fills.
Reuse math only after checking its formula against the source. Intraday,
profile, derivative and external-data branches require their own historical
input contracts rather than a daily-price approximation.

Validation commands:

```bash
.venv/bin/pytest -q core/test_experiments.py web/test_experiments_api.py
node web/test_experiments.cjs
```
