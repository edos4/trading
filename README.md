# Trading Bot v2 — Edwin & Toby
### TradingView data → Indicator analysis → Vision confirmation → IBKR execution

> **Configured for swing trading**: patterns run on daily/weekly bars, the
> scanner polls hourly (no need for minute-by-minute polling since new bars
> only print once a day/week), and position sizing/risk limits assume fewer,
> larger, multi-day-to-multi-week holds rather than rapid intraday turnover.

No webhooks. No external triggers. The bot polls TradingView on its own schedule,
detects patterns autonomously, and confirms them visually before placing any order.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SCAN LOOP  (every N seconds)                  │
│                                                                  │
│  TradingView MCP + screener (no Yahoo)                            │
│       │  OHLCV + 50+ indicators per symbol/timeframe            │
│       ▼                                                          │
│  OHLCV Store  ──────────────────────────────────────────────┐   │
│  (rolling 200-bar history per symbol/timeframe)             │   │
│       │                                                      │   │
│       ▼                                                      │   │
│  Pattern Module  (one file per Toby pattern)                │   │
│    analyze(snapshot, store) → TradeSignal | None            │   │
│       │                                                      │   │
│       │  confidence ≥ threshold?                             │   │
│       ▼                                                      │   │
│  Chart Renderer  (mplfinance PNG)  ◀────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  Vision Checker  (Claude vision API)                            │
│    CONFIRM / REJECT / UNCERTAIN                                  │
│       │                                                          │
│       │  CONFIRM only                                            │
│       ▼                                                          │
│  Risk Guard  (position size, daily loss, max positions)          │
│       │                                                          │
│       ▼                                                          │
│  Order Manager  →  Interactive Brokers (IBKR)                   │
└─────────────────────────────────────────────────────────────────┘
```

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
```

The `--pattern` flag does case-insensitive substring matching against registered
pattern names (e.g. `double_top`, `head_and_shoulders`, `rounding`). Only matching
patterns run, making it easy to evaluate individual pattern performance.

Results are saved as `backtest_results_<timestamp>.txt` (summary) and `.json`
(full trade list) in the project root.

### Comparing all patterns

To find which pattern has the lowest/highest win rate, run the comparison
script — it backtests each pattern individually and prints a sorted table:

```bash
# Default: 50 symbols per pattern (fast)
python scripts/compare_patterns.py

# More symbols for better stats
python scripts/compare_patterns.py --symbols 100

# Quick sniff
python scripts/compare_patterns.py --symbols 20
```

The table is sorted by win rate (worst first), so the weakest pattern is
at the top. Each row shows signals, trades, win/loss counts, equal-weighted
and account-weighted P&L, average P&L, max drawdown, and Sharpe ratio.
A detailed trade list follows each pattern's summary.

## Symbol Explorer UI

Launch the native desktop UI with:

```bash
python main.py --ui
```

The UI uses `tkinter`, so it runs as a local Python desktop app on Windows,
macOS, and Linux. It does not require a browser or web server.

What it supports:

- Explore top TradingView screener symbols and filter the list by ticker.
- Click a symbol to fetch daily or weekly OHLCV history and render a
  TradingView-style candlestick chart.
- Run all registered pattern modules for the selected symbol/timeframe.
  If a pattern is detected, its chart annotations are plotted on the graph
  and the signal appears in the detected-patterns table.
- Download the selected symbol's OHLCV data as CSV.
- Save the current annotated chart as PNG.

The UI reuses the same data, pattern, and chart-rendering code as the scanner,
but it does not start the scan loop or place trades.

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
│   └── backtester.py                    # Historical walk-forward backtest engine
│
├── ui/
│   └── app.py                           # Native tkinter symbol explorer
│
├── scripts/
│   └── compare_patterns.py             # Cross-pattern comparison backtest
│
└── utils/
    └── logger.py                        # Structured logging (console + files)
```

## Two-Pass Signal Verification

Every trade must pass **both** gates:

| Gate | What it checks | Blocks if... |
|------|---------------|--------------|
| Indicator analysis | EMA crossovers, RSI, volume, TV recommendation | Confidence score below threshold |
| Vision confirmation | Claude looks at the actual chart image | Pattern not visually present |

Then risk_guard adds hard limits before any order fires.

## Safety Rules
- Always start with `TRADING_MODE=paper`
- Only switch to `live` after Toby signs off on the pattern in paper trading
- All charts saved to `/charts/` for Toby's review
- All trades logged to `logs/trades.log` permanently
