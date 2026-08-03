# Trading Bot v2 — Edwin & Toby
### TradingView data → Pattern analysis → Kronos gate → Volume gate → Vision confirmation → IBKR execution

> **Configured for swing trading**: patterns run on daily/weekly bars, the
> scanner polls hourly (no need for minute-by-minute polling since new bars
> only print once a day/week), and position sizing/risk limits assume fewer,
> larger, multi-day-to-multi-week holds rather than rapid intraday turnover.

No webhooks. No external triggers. The bot polls TradingView on its own schedule,
detects patterns autonomously, and confirms them (Kronos forecast, optional
volume confirm, optional vision) before placing any order.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SCAN LOOP  (every N seconds)                  │
│                                                                  │
│  TradingView MCP + screener (no Yahoo)                            │
│       │  OHLCV + 50+ indicators per symbol/timeframe            │
│       ▼                                                          │
│  OHLCV Store  ──────────────────────────────────────────────┐   │
│  (rolling history per symbol/timeframe)                     │   │
│       │                                                      │   │
│       ▼                                                      │   │
│  Pattern Module  (one file per Toby pattern)                │   │
│    analyze(snapshot, store) → TradeSignal | None            │   │
│       │                                                      │   │
│       ▼                                                      │   │
│  Kronos 1w gate  (optional, default ON)                     │   │
│    forecast agrees with BUY/SELL + |move| ≥ min?             │   │
│       │                                                      │   │
│       │  PASS                                                │   │
│       ▼                                                      │   │
│  Volume gate  (optional, default OFF)                       │   │
│    RVOL ≥ min + OBV slope agrees with BUY/SELL?              │   │
│       │                                                      │   │
│       │  PASS                                                │   │
│       ▼                                                      │   │
│  Chart Renderer  (mplfinance PNG)  ◀────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  Vision Checker  (Claude vision API, optional)                  │
│    CONFIRM / REJECT / UNCERTAIN                                  │
│       │                                                          │
│       │  CONFIRM only (when enabled)                             │
│       ▼                                                          │
│  Risk Guard  (position size, daily loss, max positions)          │
│       │                                                          │
│       ▼                                                          │
│  Order Manager  →  Interactive Brokers (IBKR)                   │
└─────────────────────────────────────────────────────────────────┘
```

Kronos and the volume gate are **confirm/veto layers** on top of chart
patterns — they do **not** generate entries on their own. Both run in CLI
scan/paper/backtest and in the `--ui` explorer, Backtest dialog, and Paper
Trading dashboard.

## Setup

### 1. Create and activate a virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate    # Windows
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

Requires `tradingview-mcp-server` (stdio MCP) and `tradingview-screener`.
Set `TV_HISTORY_DAYS` in `.env` to control how many daily bars the screener pulls (default 60, max 200).

### 3. Configure environment
```bash
cp .env.example .env
# Fill in: IBKR settings, ANTHROPIC_API_KEY, WATCHLIST
# Optional Kronos gate: KRONOS_GATE_ENABLED / KRONOS_MIN_MOVE_PCT
#   (needs ~/Kronos weights — see "Kronos Confirm Gate" below)
# Optional Volume gate: VOLUME_GATE_ENABLED / VOLUME_GATE_RVOL_MIN
#   (default OFF — see "Volume Confirm Gate" below; measure with
#    --volume-gate-compare before enabling)
```

### 4. Start TWS or IB Gateway
- Paper trading port: **7497** | Live: **7496**
- Enable API: Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients

### 5. Run
```bash
python main.py
```

## Backtesting

Test the strategy against historical data (no live connection needed):

```bash
# Full backtest — all patterns, top 100 symbols
python main.py --backtest

# Quick test — top 10 symbols
python main.py --backtest 10

# Single-pattern test — isolate one pattern for focused tuning
python main.py --backtest --pattern double_top
python main.py --backtest 10 --pattern channel

# Force volume confirm gate ON for this run (overrides VOLUME_GATE_ENABLED=false)
python main.py --backtest --volume-gate

# A/B: same symbols/patterns with volume gate OFF then ON
python main.py --backtest 100 --volume-gate-compare
```

The `--pattern` flag does case-insensitive substring matching against registered
pattern names (e.g. `double_top`, `head_and_shoulders`, `rounding`). Only matching
patterns run, making it easy to evaluate individual pattern performance.

Results are saved as `backtest_results_<timestamp>.txt` (summary) and `.json`
(full trade list) in the project root. `--volume-gate-compare` also writes
`backtest_volume_ab_<timestamp>.json` with side-by-side OFF vs ON metrics.

When `KRONOS_GATE_ENABLED=true`, the backtester applies the same Kronos 1w
confirm gate as live/paper (see [Kronos Confirm Gate](#kronos-confirm-gate)).
The volume gate is **off by default** — see
[Volume Confirm Gate](#volume-confirm-gate).

### Comparing all patterns

To find which pattern has the lowest/highest win rate, run the comparison
script — it backtests each pattern individually and prints a sorted table:

```bash
# Default: 50 symbols per pattern, parallel=CPU count (fast)
python scripts/compare_patterns.py

# More symbols for better stats
python scripts/compare_patterns.py --symbols 100

# Control concurrency — limit to N simultaneous backtests
python scripts/compare_patterns.py -p 2

# Quick sniff
python scripts/compare_patterns.py --symbols 20
```

The table is sorted by win rate (worst first), so the weakest pattern is
at the top. Each row shows signals, trades, win/loss counts, equal-weighted
and account-weighted P&L, average P&L, max drawdown, and Sharpe ratio.
A detailed trade list follows each pattern's summary.

## Kronos Confirm Gate

[Kronos](https://github.com/shiyu-coder/Kronos) is an open-source foundation
model for financial candlesticks (K-lines). This project uses
[Kronos-base](https://huggingface.co/NeoQuasar/Kronos-base) as a **1-week
forecast filter** on chart-pattern signals — not as a standalone trading
pattern.

### Why a gate (not an entry pattern)

Toby chart patterns stay the entry source. Kronos is only a confirm/veto
layer: *"does your 1w forecast agree with this BUY/SELL, and is the
predicted move large enough to matter?"* It does not generate entries on
its own.

This is **not** the Kronos repo's finetune top-K backtest demo. We call the
same `KronosPredictor.predict()` API; strategy layering is ours.

### What the gate does

Implemented in `core/kronos_gate.py`. After a pattern emits BUY/SELL on the
`1d` timeframe:

1. Feed the last **400 daily bars** (official Kronos lookback) of OHLCV plus
   synthetic `amount = volume * mean(OHLC)` when the feed has no turnover.
2. Forecast the next **5 trading days** of close (`pred_len=5` — a 1w veto
   horizon; Kronos demos often use 120 for longer charts).
3. Average `KRONOS_SAMPLE_COUNT` sampled paths (`T=1.0`, `top_p=0.9`).
4. **Reject** if `|pred_1w| < KRONOS_MIN_MOVE_PCT` (default 6%).
5. **Reject** if the forecast direction conflicts with the signal
   (BUY needs `pred_1w > 0`, SELL needs `pred_1w < 0`).
6. On **PASS**, optionally overwrite take-profit / stop-loss from the
   forecast (`KRONOS_GATE_ADJUST_EXITS=true`).

Fail-open: if `~/Kronos` weights are missing, history is shorter than
lookback, or `predict()` errors, the signal is allowed through (with a
one-time warning) so a broken install cannot freeze the scanner.

Skipped: `CLOSE` actions and non-daily timeframes.

### Where it runs

| Entry point | How the gate is controlled |
|---|---|
| `python main.py` (live/paper scan) | `KRONOS_GATE_ENABLED` in `.env` |
| `python main.py --paper` | same |
| `python main.py --backtest` | same |
| `python main.py --ui` → symbol explorer | toolbar checkbox **Kronos 1w gate** |
| `python main.py --ui` → **Backtest** | form checkbox **Kronos 1w gate** |
| `python main.py --ui` → **Paper Trading** | toolbar checkbox **Kronos 1w gate** |

UI checkboxes default to the `.env` value but can override for that session.
Startup logs print `Kronos gate: ON/OFF`.

### `.env` settings

```bash
# Require Kronos 1w forecast to agree with chart-pattern BUY/SELL
KRONOS_GATE_ENABLED=true
# Overwrite TP/SL from the 1w forecast when the gate passes (off by default)
KRONOS_GATE_ADJUST_EXITS=false
# Minimum |predicted 1w move| to pass (0.06 = 6%)
KRONOS_MIN_MOVE_PCT=0.06
# Average N sampled forecast paths per prediction (reduces noise)
KRONOS_SAMPLE_COUNT=3
# Prefer finetuned weights under ~/Kronos/finetuned (falls back to base)
KRONOS_USE_FINETUNED=false
# Daily history pull — must be ≥400 for full official lookback (clamped ≤512)
TV_HISTORY_DAYS=450
```

Disable anytime with `KRONOS_GATE_ENABLED=false` (or uncheck in the UI).

### One-time weight setup

Required for the gate (and for `--kronos-test` / `--kronos-finetune`):

```bash
git clone https://github.com/shiyu-coder/Kronos.git ~/Kronos
```

Download and cache the weights locally (avoids hitting Hugging Face on every run):

```bash
python3 -c "
import sys; sys.path.insert(0, '$HOME/Kronos')
from model import Kronos, KronosTokenizer
KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base', token=False) \
    .save_pretrained('$HOME/Kronos/weights/Kronos-Tokenizer-base')
Kronos.from_pretrained('NeoQuasar/Kronos-base', token=False) \
    .save_pretrained('$HOME/Kronos/weights/Kronos-base')
"
```

### Forecast accuracy test (`--kronos-test`)

Walk-forward scores of Kronos +1 day / +1 week close forecasts on the
historical daily CSVs in `/home/r00t/stocks_data`. Useful after fine-tuning
or when comparing weight sets — not a published accuracy claim for the
live gate.

```bash
# Default: 20 randomly sampled symbols, 3 walk-forward windows each
python main.py --kronos-test

# 50 symbols
python main.py --kronos-test 50

# Rank by recent dollar volume and take the top 50 (liquid large/mid-caps)
# instead of a random sample — Kronos was trained on liquid exchange data,
# so this is a fairer test than a random sample dominated by illiquid/
# penny/OTC tickers by sheer count.
python main.py --kronos-test 50 --kronos-liquid-only

# After fine-tuning (see below)
python main.py --kronos-test 50 --kronos-liquid-only --kronos-use-finetuned
```

Output reports, per horizon: `n`, `MAE`, `naive MAE` (flat 0% baseline),
and `direction hit` (% of windows where predicted up/down matched reality).

### Fine-tuning (`--kronos-finetune`)

Adapts Kronos-base's tokenizer + predictor on liquid tickers from
`/home/r00t/stocks_data`. Saves checkpoints under `~/Kronos/finetuned/`.
Needs a CUDA GPU to be practical. Re-score with
`--kronos-test ... --kronos-use-finetuned` before switching the live gate
onto fine-tuned weights (the live gate currently loads **base** weights
by design).

```bash
python main.py --kronos-finetune
python main.py --kronos-finetune --kronos-finetune-symbols 1500 --kronos-finetune-epochs 10
```

## Volume Confirm Gate

Price-volume confirmation for chart-pattern signals. Implemented in
`analysis/price_volume.py`. Like Kronos, this is a **confirm/veto layer** —
it does **not** generate entries. Flag / pennant / double top-bottom already
embed their own volume rules; this gate adds a uniform RVOL + OBV check on
top of every pattern (including those).

**Default: OFF.** Enable only after `--volume-gate-compare` shows an expectancy
/ profit-factor edge without collapsing trade count (rule of thumb: keep at
least ~50% of the baseline sample).

### In plain English

A chart pattern can look perfect on price alone while almost nobody is
actually trading it. The volume gate asks two simple questions before a
BUY or SELL is allowed through:

1. **Is today’s volume loud enough?**  
   Compare today’s share volume to a quiet “normal” level (the average of
   the previous ~20 days). By default the day must be at least **1.5×** that
   normal. Quiet days get rejected — the idea is that real breakouts usually
   come with more people participating, not a whisper.

2. **Is money flowing the same way as the trade?**  
   OBV (On-Balance Volume) is a running score: volume is added on up days and
   subtracted on down days. Over the last few days (default **5**), that score
   should be **rising for a BUY** (buyers showing up) and **falling for a
   SELL** (sellers showing up). If price wants to go up but volume has been
   leaking the other way, the gate says no.

Both must pass. If history is too short to judge (not enough volume bars),
the gate **lets the trade through** rather than guessing — better a possible
trade than a frozen bot.

It never invents trades. A pattern still has to fire first; this only vetoes
weak-volume ones.

### What the gate does (technical)

After a pattern emits BUY/SELL (and after the Kronos gate when that is on):

1. **RVOL** — signal-bar volume / SMA(volume, 20 of prior bars) must be
   ≥ `VOLUME_GATE_RVOL_MIN` (default **1.5**).
2. **OBV direction** — OBV slope over the last `VOLUME_GATE_OBV_BARS` bars
   (default **5**) must agree with the trade: BUY needs slope ≥ 0, SELL needs
   slope ≤ 0.
3. **Fail-open** if fewer than 20 volume bars (or RVOL/OBV unavailable) so
   thin data cannot freeze the scanner.

Skipped: `CLOSE` actions.

Accepted trades are tagged with `rvol` / `obv_slope` on `TradeSignal` /
`BacktestTrade` for post-analysis even when the gate is off (backtest still
records the metrics).

### Where it runs

| Entry point | How the gate is controlled |
|---|---|
| `python main.py` (live/paper scan) | `VOLUME_GATE_ENABLED` in `.env`, or `--volume-gate` for this run |
| `python main.py --paper` | same |
| `python main.py --backtest` | same; add `--volume-gate-compare` for A/B |
| `python main.py --ui` → symbol explorer | toolbar checkbox **Volume gate** |
| `python main.py --ui` → **Backtest** | form checkbox **Volume gate (RVOL+OBV)** + **Compare A/B (Volume)** |
| `python main.py --ui` → **Paper Trading** | toolbar checkbox **Volume gate** (rejection count in scan stats) |

UI checkboxes default to the `.env` value but can override for that session.
Startup logs print `Volume gate: ON/OFF`. Rejects log as
`Volume gate REJECT | SYM pattern | reason`.

### `.env` settings

```bash
# Off until A/B shows an edge — do not enable blindly
VOLUME_GATE_ENABLED=false
# Signal-bar volume must be at least this multiple of the 20-bar average
VOLUME_GATE_RVOL_MIN=1.5
# Bars used for OBV slope (BUY ≥ 0, SELL ≤ 0)
VOLUME_GATE_OBV_BARS=5
```

### Measuring before enabling

```bash
# Side-by-side OFF vs ON (same symbols/patterns). Prefer Kronos off if you
# want to isolate the volume effect:
KRONOS_GATE_ENABLED=false python main.py --backtest 100 --volume-gate-compare

# Per-pattern isolation
python main.py --backtest 100 --pattern double_bottom --volume-gate-compare
```

Compare trades, win rate, avg R, expectancy, profit factor, and max drawdown.
Enable in `.env` only if expectancy / PF improve **and** trade count does not
collapse. A run that goes to zero trades is not an edge — it is sample death.

## Symbol Explorer UI

### Desktop (tkinter)

```bash
python main.py --ui
```

Local desktop app on Windows/macOS/Linux. No browser required.

### Web (VPS)

Authenticated browser UI with the same Explorer / Backtest / Paper surfaces:

```bash
# Required in .env — server refuses to start if WEB_UI_PASSWORD is empty
WEB_UI_USERNAME=admin
WEB_UI_PASSWORD=change-me-to-a-long-random-secret
# WEB_UI_SECRET_KEY=...   # recommended in production
# WEB_UI_HTTPS=true       # set when behind TLS so the session cookie is Secure

pip install -r requirements.txt
python main.py --web
# → http://0.0.0.0:8080  (login required)
```

Auth: form login → signed HttpOnly session cookie (`tb_session`). All pages and
`/api/*` routes require a valid session. `/login`, `/logout`, `/health`, and
`/static/*` are public. Put nginx/Caddy TLS in front for VPS deploys and set
`WEB_UI_HTTPS=true`.

What both UIs support:

- Explore top TradingView screener symbols and filter the list by ticker.
- Click a symbol to fetch daily or weekly OHLCV history and render a
  TradingView-style candlestick chart.
- Run all registered pattern modules for the selected symbol/timeframe.
  If a pattern is detected, its chart annotations are plotted on the graph
  and the signal appears in the detected-patterns table.
- **Kronos 1w gate** checkbox (toolbar): when checked, explorer detections
  must also clear the same Kronos confirm gate used by the scanner.
- **Volume gate** checkbox (toolbar): when checked, detections must also
  clear the RVOL+OBV confirm gate (see [Volume Confirm Gate](#volume-confirm-gate)).
- **Backtest**: parameter form (includes **Kronos 1w gate** and **Volume gate**,
  plus **Compare A/B (Volume)**) runs `Backtester` with the same engine as
  `python main.py --backtest`.
- **Paper Trading**: live paper dashboard that runs `MarketScanner` with a
  `PaperAccount` (same path as `python main.py --paper`).
- Download the selected symbol's OHLCV data as CSV / save chart PNG.

The explorer reuses the same data, pattern, chart-rendering, Kronos-gate,
and volume-gate code as the scanner. Backtest / Paper Trading are not toy
modes — they call the real `Backtester` / `MarketScanner`.

## Adding a New Pattern

1. Create `patterns/pattern_00X_name.py`
2. Subclass `BasePattern`
3. Set `name`, `timeframes`, and `chart_description`
4. Implement `analyze(snapshot, store) → TradeSignal | None`
5. Return a `TradeSignal` with a meaningful `confidence` score
6. Restart the bot — auto-discovered, no other changes needed

## Project Structure

```
trading_bot_v2/
├── main.py                              # Entry point — just runs the scanner
├── config.py                            # All settings from .env
├── requirements.txt
├── .env.example
│
├── data/
│   ├── tv_client.py                     # TradingView MCP + screener fetcher
│   └── ohlcv_store.py                   # Rolling candle history per symbol/timeframe
│
├── analysis/
│   ├── indicator_engine.py              # EMA, RSI, MACD, BB, ATR, OBV, VWAP...
│   ├── price_volume.py                  # RVOL + OBV volume confirm gate
│   ├── chart_renderer.py                # mplfinance candlestick chart → PNG
│   └── vision_checker.py               # Claude vision API confirmation
│
├── patterns/
│   ├── base_pattern.py                  # Abstract base — analyze() interface
│   └── pattern_001_ema_crossover.py     # Template / first pattern
│
├── broker/
│   ├── ibkr_client.py                   # IBKR connection + market data
│   └── order_manager.py                 # Order placement & fill tracking
│
├── risk/
│   └── risk_guard.py                    # Pre-trade checks (size, daily loss, limits)
│
├── core/
│   ├── scanner.py                       # Main scan loop — ties everything together
│   ├── backtester.py                    # Historical walk-forward backtest engine
│   ├── test_volume_gate.py              # Unit tests for analysis.price_volume
│   ├── kronos_eval.py                   # Kronos-base forecast accuracy test (--kronos-test)
│   ├── kronos_gate.py                   # Kronos 1w confirm gate for chart-pattern signals
│   └── kronos_finetune.py               # Fine-tune Kronos on liquid tickers (--kronos-finetune)
│
├── ui/
│   ├── app.py                           # Native tkinter symbol explorer (--ui)
│   ├── backtest_dialog.py               # UI Backtest launcher (Kronos + Volume gates, A/B)
│   └── paper_dashboard.py               # UI Paper Trading dashboard (Kronos + Volume gates)
│
├── web/
│   ├── app.py                           # Authenticated FastAPI web UI (--web)
│   ├── auth.py                          # Session login (WEB_UI_PASSWORD required)
│   ├── services.py                      # Explorer data/pattern/chart helpers
│   └── jobs.py                          # Background backtest + paper session
│
├── scripts/
│   └── compare_patterns.py             # Cross-pattern comparison backtest
│
└── utils/
    └── logger.py                        # Structured logging (console + files)
```

## Signal Verification

Every trade can pass up to **four** gates (then risk limits):

| Gate | What it checks | Blocks if... | Default |
|------|----------------|--------------|---------|
| Pattern / indicator analysis | Chart structure + indicators → `TradeSignal` | No signal / confidence below engine threshold | always on |
| Kronos 1w confirm | Forecast direction + `|pred_1w| ≥ KRONOS_MIN_MOVE_PCT` | Forecast disagrees or move too small | `KRONOS_GATE_ENABLED=true` |
| Volume confirm | RVOL ≥ `VOLUME_GATE_RVOL_MIN` + OBV slope agrees with BUY/SELL | Weak volume or OBV against the trade | `VOLUME_GATE_ENABLED=false` |
| Vision confirmation | Claude looks at the chart PNG | Pattern not visually present | `VISION_CONFIRMATION_ENABLED=false` |

Then risk_guard / paper sizing add hard limits before any order fires.
Order: pattern → Kronos → Volume → vision. Kronos and volume run **before**
vision so rejected forecasts/volume do not spend Claude tokens.

## Safety Rules
- Always start with `TRADING_MODE=paper`
- Only switch to `live` after Toby signs off on the pattern in paper trading
- All charts saved to `/charts/` for Toby's review
- All trades logged to `logs/trades.log` permanently
