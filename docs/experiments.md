# Historical Experiments

## Create an experiment from one URL

On **Experiments**, paste a public video link into **Create an experiment from a
video** and click **Generate experiment**. There is no `list.md` step. Supported
sites are Facebook, YouTube, Vimeo, Instagram, TikTok and X; site access and
extractor support still determine whether a particular link can be downloaded.

Generation saves the video, fully decodes its audio/video and records a SHA-256,
then prepares the complete local speech transcript, caption and timestamped
original frames. The selected vision model reads those inputs and
writes `writeup.md` and `33ai-integration.md`, matching the archived folder
structure. It can request one further pass of up to twelve original frames to
resolve a settings panel between the uniform samples. This is AI interpretation
of sampled frames, not a claim that every frame was watched or a human review.

The writeup covers what the source does, reproduction steps, settings with
timestamps, omissions, contradictions and evaluation. The integration plan
documents inputs, application integration, historical data, validation and
remaining gaps. Inaccessible, silent, truncated, promotional and non-trading
sources retain their actual limitations. No missing formula, stop, target,
timeframe or proprietary output is invented to complete a strategy.

Each URL has a persistent job and its own folder under
`EXPERIMENTS_STORE_DIR/sources/url-<hash>/`. Progress and files survive a browser
reload. Cancel retains evidence; Retry is explicit after failure, cancellation
or interruption. A cross-process lock permits one media/analysis worker at a
time, with up to eight queued/active jobs. Worker leases and attempt fencing
prevent an expired or cancelled worker from publishing into a retry. A URL
already in the archived catalog reuses its reviewed experiment and locally
available source files. Tracking parameters do not create duplicate jobs.

Click **Open experiment**, open **Source & plan** to watch the saved video
beside the writeup and integration plan, select dates/market and click
**Start selected experiment**. Generating an experiment does not start a
historical run. **All folders** remains available for the original batch flow.
Saved batches retain frozen source lists and documents when new URLs are added.

### Automatically runnable scope

Complete sources can generate `rules.json`, a validated data-only specification
consumed by `core/experiments/rules.py`. It supports explicit daily-equity EMA,
SMA, rolling high/low and Wilder ATR features, nonnegative lags, comparisons,
fresh crosses, and bounded AND/OR combinations. Entries and exits must both be
defined, with source evidence. There is no `eval`, generated Python, dynamic
import or shell command in this contract.

The execution book uses completed daily signals, next-session-open fills,
independent fixed-notional positions, one position per symbol/variant and
signal-condition exits. Existing fees, liquidity, PH ticks/lots, borrow policy,
holdout and end handling apply. This declared research account does not become
the source's portfolio sizing or a human-reviewed adapter. Brackets, intrabar
orders, trailing stops, risk-based sizing, intraday/derivative/profile/external
inputs, proprietary/repainting signals and discretionary patterns are outside
this compiler. A source that requires them gets its documents and a specific
blocker. Unsupported source filters or exits are never silently discarded.

Source/plan and rule hashes are checked before new runs. The batch stores the
full specification and its evidence alongside the original source interpretation.
Changing a generated rule or document without matching integrity records blocks
it, while previously saved runs retain their frozen version.

### Server setup

The downloader/transcriber is isolated from the main application and Laya:

```bash
python3 -m venv .venv-experiments
.venv-experiments/bin/pip install -r requirements-experiments.txt
.venv-experiments/bin/python scripts/experiment_media.py --check
```

`EXPERIMENTS_MEDIA_PYTHON` defaults to `.venv-experiments/bin/python`.
`EXPERIMENTS_GENERATION_PROVIDER` defaults to `deepseek`, reusing the existing
`DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` and vision-capable `DEEPSEEK_MODEL` (Flash).
[DeepSeek documents native frame/image support](https://api-docs.deepseek.com/guides/vision/)
through its Anthropic-compatible API; Pro is rejected because it does not read
images. Selecting `anthropic` instead uses `ANTHROPIC_API_KEY` and the existing
Claude vision model, independently of `VISION_CONFIRMATION_ENABLED`. There is
no silent provider fallback after an error. The optional vision-model override is
`EXPERIMENTS_GENERATION_MODEL` (blank uses the selected provider's model);
`EXPERIMENTS_GENERATION_TIMEOUT_SECONDS` defaults to 300 seconds per analysis
call. Model/request identity and usage are retained in each generation record.
No key is returned to the browser or passed to the media child process. Settings
are exposed under **URL experiments**; changing storage requires a restart.

The worker accepts recorded videos up to one hour and 512 MiB. Speech weights
are downloaded into the experiment source cache on first use. Public HTTP(S)
requests, extractor redirects and DNS results are checked; local/private
addresses, credentials in URLs, custom ports, playlists and live streams are
rejected. No browser cookies or account credentials are imported. Readable
errors retain the evidence and identify what must be retried or supplied.

All import, status, retry, cancel and file endpoints require the existing web
login. File paths are confined to that job's directory. New URLs do not modify
`list.md`, the archived source bundle, history, Paper books or the immutable
pattern registry.

The generation API is `POST /api/experiments/imports` with `{"url":"…"}`;
poll `GET /api/experiments/imports/{id}`. New historical runs optionally accept
`source_ids` to run only selected experiments; an empty selection means all.

Validation:

```bash
.venv/bin/pytest -q core/test_experiment_imports.py core/test_experiments.py web/test_experiments_api.py
node web/test_experiments.cjs
```

## Archived experiments

Open **Experiments**, select US or PH, supply inclusive start/end session dates
and click **Start all folders**. A batch creates a separate result record for
each of the 449 archived writeups. Search or filter folders, then click one to
view positions, closed trades, signals, performance, event outcomes, logs and
**Source & plan**. Completed folders show their variant's **Net P&L** in the
sidebar and are sorted by it, highest first; folders with multiple variants
report their best variant and name it. Unrun, blocked and event-study folders
keep catalog order below them. That tab plays the folder's saved video, with
the writeup and integration plan beside it. Click a symbol to open the same
chart viewer used by Paper, using this batch's frozen data and saved record
levels. Complete folder evidence is downloadable as JSON Lines once the folder
finishes; failed/cancelled/interrupted folders export their explicitly labeled
retained partial results.

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


### Daily execution capacity

The research proxy caps entries at 1% of the mean of up to 20 prior daily volumes and the corresponding mean traded value divided by prior close, using the tighter limit. A smaller entry cancels its remainder. Exits require sufficient capacity for the remaining position and retry at subsequent opens. The recorded full-day volume can invalidate an impossible fill after the fact, but never determines order size. The model has no auction depth or bid/ask history. Fees and slippage are explicit assumptions.


PH execution rounds buys up and sells down to a valid price tick before applying the dated board lot. The default broker scenario uses 0.3006% per leg before seller tax (0.25% commission plus VAT and exchange, regulatory and clearing charges). Seller tax is 0.5% before 2018, 0.6% through June 2025 and 0.1% thereafter. An explicit per-side override replaces this schedule. [PSE example](https://www.pse.com.ph/investing-at-pse/) · [2018 tax regulation](https://elibrary.judiciary.gov.ph/thebookshelf/showdocs/10/89788) · [2025 PSE circular](https://documents.pse.com.ph/CircularOPSPDF/CN-2025-0026.pdf)
