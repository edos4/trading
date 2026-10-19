# Trading Bot v2 — 33AI

> **Configured for swing trading**: patterns run on daily/weekly bars, the
> scanner polls hourly (no need for minute-by-minute polling since new bars
> only print once a day/week), and position sizing/risk limits assume fewer,
> larger, multi-day-to-multi-week holds rather than rapid intraday turnover.

No webhooks. No external triggers. The bot polls TradingView on its own schedule,
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
│    forecast agrees with BUY/SELL + |move| ≥ 3% in 3 days?    │   │
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
# Volume gate: VOLUME_GATE_ENABLED / VOLUME_GATE_RVOL_MIN (default ON)
```

### 4. Start TWS or IB Gateway
- Paper trading port: **7497** | Live: **7496**
- Enable API: Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients

### 5. Run
```bash
python main.py
```

## Paper trading (CLI)

`python main.py --paper` runs the live scanner against a virtual account
(simulated fills, no broker). Universe size can be `--paper 50` or
`--paper --symbols=500`. Add `--paper-reset` to wipe the saved ledger first.

Universe size is not capped at an arbitrary ceiling: request as many names as
the market quotes (US ≈7.9k, PH ≈244) and the screener returns what it has.
Existing liquidity/ADV floors still trim the tradeable set. The web Paper tab's
**Symbols** field honors the same range.

Historical replay (markets closed, or a dated walk-forward) uses `--stream`.
That flag starts the paper-trade stream server if needed, then the scanner
reads bars from it instead of live TradingView. Dates are **US `MM/DD/YYYY`**
or `YYYY-MM-DD` — `01/05/2026` is 5 January 2026.

```bash
# Pattern-only paper stream: top 500 names, collect-then-open the best 4
# setups by reward:risk each scan, dump trades when the process exits
python main.py --paper --symbols=500 --pattern-only --collect-first=4 \
    --stream=01/05/2026 --duration-days=30 --export-trades-log=output_trades.json
```

| Flag | What it does |
|---|---|
| `--paper` | Paper ledger + scanner (optional `N` is universe size, default 50) |
| `--symbols=N` | Universe size; overrides the optional `N` on `--paper` / `--backtest` |
| `--collect-first=N` | Collect chart-pattern hits for the scan, rank by R:R, open only the top N (default `COLLECT_FIRST_TOP_N`). Bare `--collect-first` uses the `.env` default |
| `--stream=DATE` | Replay daily bars from DATE. Auto-starts `main.py --papertrade-stream`. Resume a saved ledger from its last sim date when that date is later than DATE |
| `--duration-days=N` | Stop after N unique market sessions (with `--stream`, N replayed daily bars). `--stream=01/05/2026 --duration-days=30` replays ~30 trading days then exits |
| `--export-trades-log=PATH` | On exit (including Ctrl+C after the scan loop unwinds), write open + closed trades JSON — same schema as the UI **Export Trades** button |

`--stream`, `--duration-days`, and `--export-trades-log` require `--paper`. You can still run the
stream server on its own with `python main.py --papertrade-stream` and point
the UI at it; the combined `--paper --stream=...` form is for unattended CLI
replays.

`--collect-first --collect-first-top-n 4` still works; `--collect-first=4` is
the short form.

## Backtesting

Test the strategy against historical data (no live connection needed):

```bash
# Full backtest — enabled patterns, top 50 US names (dollar volume + $20M ADV)
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

## Volume Confirm Gate


Price-volume confirmation for chart-pattern signals, in `analysis/price_volume.py`. It does **not** generate entries. Flag / pennant / double top-bottom already
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
| `python main.py --ui` → **Paper Trading** | per-book **Volume gate** (rejection count in that book’s scan stats) |

UI checkboxes default to the `.env` value but can override for that session.
Startup logs print `Volume gate: ON/OFF`. Rejects log as
`Volume gate REJECT | SYM pattern | reason`.

### `.env` settings

```bash
VOLUME_GATE_ENABLED=true
# Signal-bar volume must be at least this multiple of the 20-bar average
VOLUME_GATE_RVOL_MIN=1.5
# Bars used for OBV slope (BUY ≥ 0, SELL ≤ 0)
VOLUME_GATE_OBV_BARS=5
```

### Measuring before enabling

```bash
python main.py --backtest 100 --volume-gate-compare
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

Authenticated browser UI with the same Explorer / Backtest / Paper surfaces (plus
Patterns, Indicators, Laya, Kronos, Documentation, and a Trello-style **Tasks**
board). The **Kronos** tab forecasts N trading days ahead for one or more
symbols — comma-separated (`AAPL, NVDA, MSFT`, spaces ignored) — or for the
**top X NASDAQ stocks** by dollar volume (enter X), and draws one chart per
symbol:

```bash
# Required in .env — server refuses to start if WEB_UI_PASSWORD is empty
WEB_UI_USERNAME=admin
WEB_UI_PASSWORD=change-me-to-a-long-random-secret
# WEB_UI_SECRET_KEY=...   # recommended in production
# WEB_UI_HTTPS=true       # set when behind TLS so the session cookie is Secure
# TASKS_ENABLED=true      # Trello-style Tasks tab (PostgreSQL-backed)

pip install -r requirements.txt
python main.py --web
# → http://0.0.0.0:8080  (login required)
```

Auth: form login → signed HttpOnly session cookie (`tb_session`). All pages and
`/api/*` routes require a valid session **or HTTP Basic** (same
`WEB_UI_USERNAME` / `WEB_UI_PASSWORD`, plus any extra named logins in
`WEB_UI_USERS`). `/login`, `/logout`, `/health`, and
`/static/*` are public. Put nginx/Caddy TLS in front for VPS deploys and set
`WEB_UI_HTTPS=true`. The session cookie is also flagged `Secure` automatically
whenever a request arrives over TLS (`X-Forwarded-Proto: https`), so no extra
config is needed behind a TLS-terminating proxy or the Cloudflare Tunnel below.

### Laya text assessment (optional, seventh tab)

A local, optional assessment service with its own research workspace
(`Explorer | Backtest | Paper | Replay | Patterns | Indicators | Laya`). Laya
classifies pasted financial text (event review) and reviews evidence around an
existing candidate (candidate review). It never places orders, changes
stops/targets, or manages exits — its answer is additional evidence, not
authority to change trading rules.

```bash
# Isolated worker env (never add laya[mcp]: it conflicts with mcp>=1.28.0,<2)
python -m venv .venv-laya && .venv-laya/bin/pip install -r requirements-laya.txt
# Offline smoke (no checkpoint download) + benchmark report:
.venv-laya/bin/python scripts/laya_smoke.py --fake
# Versioned evaluation report (model quality vs deterministic baseline):
.venv/bin/python scripts/evaluate_laya.py --fake
```

Then provision a pinned checkpoint and set in `.env` (see `.env.example`):
`LAYA_ENABLED=true`, `LAYA_WORKER_PYTHON`, `LAYA_MODEL_PATH`. Without these
the tab renders an honest unavailable state and `/health` stays independent.
`LAYA_ENABLED` enables assessment execution only — entry filtering needs an
explicit per-book/per-run `shadow`/`filter` mode plus a validated policy, and
is paper/simulation-only.

Paper trading: each book card has a **Laya** checkbox (with Filter/Shadow mode
and a policy id, default `laya-event-v1-default`), just like Pattern-only /
Indicators-only / Patternsv2. Tick it and Start moves that book's entries
through Laya: Filter gates entries (allow/veto/abstain, paper-only), Shadow
only records assessments. The default event policy vetoes only on affirmative
danger in recent EDGAR filings (offering/dilution or material adverse text);
with no filing evidence it abstains. Starting a Laya book while the service
is not ready is refused with the reason. Full design: `docs/laya.md`.

Paper stream replays accept an optional **Duration** (market sessions) per
book card: the book stops by itself after that many unique sessions instead
of running to tape exhaustion. Blank (default) preserves the old unbounded
behavior. The card shows live progress as `Sim days: N/M`.

### Tasks (Kanban) board (optional, PostgreSQL)

A small Trello-style board on the **Tasks** tab: addable/renameable lists,
cards with a description and subtask checklist, and comments (flat, one level of
replies). Cards and comments are stamped with the **logged-in username** as
their author.

Storage is PostgreSQL in its own `tasks` schema (`TASKS_DATABASE_URL` /
`TASKS_SCHEMA`; empty reuses `DATABASE_URL` / `db_*`). Create or migrate the
schema before first use:

```bash
python scripts/setup_tasks_db.py
```

Drag and drop is touch-friendly (vendored SortableJS) with click/keyboard
equivalents. When PostgreSQL is unavailable the tab shows a readable error and
every other tab keeps serving; `TASKS_ENABLED=false` hides it. Whole board as
JSON: `GET /api/tasks/export`. Full design: `docs/kanban-plan.md`.

### Local web with a public URL (Cloudflare Tunnel) — optional, host-specific

`./run_web.sh` is a plain local server by default. On a host that opts in (by
setting `CLOUDFLARE_TUNNEL_TOKEN` **and** `CLOUDFLARE_TUNNEL_HOSTNAME` in `.env`)
it also exposes the same UI publicly through a Cloudflare Tunnel — no inbound
ports and no static IP. It uses the Cloudflare `cf` CLI (`cf auth login` once)
with a remotely-managed tunnel, so the ingress rules live in Cloudflare and only
the tunnel token is needed locally.

```bash
./run_web.sh              # local; + tunnel when .env opts in
./run_web.sh --no-tunnel  # web UI only → http://localhost:8080
```

Settings come from `.env` (see `.env.example`): `CLOUDFLARE_TUNNEL_TOKEN` and
`CLOUDFLARE_TUNNEL_HOSTNAME`. A clone without those (or `--no-tunnel`) stays
local-only, so this never affects other machines. Ctrl+C stops both.

Verify the public URL end-to-end with [browser-use](https://github.com/browser-use/browser-use):

```bash
# One-time: browser-use pins conflict with the app's deps, so it gets its own venv
python3 -m venv .venv-browseruse
.venv-browseruse/bin/pip install browser-use playwright
.venv-browseruse/bin/python -m playwright install chromium

# Local by default; pass --base-url https://<public-host> for the tunnelled URL.
.venv-browseruse/bin/python web/test_e2e_browseruse.py
```

### Keep it running across restarts (systemd)

`run_web.sh` is wrapped in a systemd **user** service so the app and tunnel come
back automatically after a reboot or crash, with no login required (login
`linger` is enabled):

```bash
scripts/install_web_service.sh              # install + enable + start
scripts/install_web_service.sh --status
scripts/install_web_service.sh --uninstall
```

The unit is `deploy/trading-web.service`, installed to
`~/.config/systemd/user/trading-web.service`, and runs
`run_web.sh --require-tunnel`. That flag makes `run_web.sh` **exit** instead of
silently serving local-only when the tunnel token or the `cf` CLI is missing, so
`Restart=always` surfaces the fault rather than hiding it. Manage it with:

```bash
systemctl --user status  trading-web.service
systemctl --user restart trading-web.service   # pick up code / .env changes
journalctl --user -u trading-web.service -f
```

**One history database (VPS).** Contabo runs Postgres `stocks_history` and
serves `GET /api/history/symbols`, `GET /api/history/{symbol}`, and
`GET /api/history/{symbol}/meta`. Local `--ui` / `--web` **always** read
backtest prefetch) from `https://33ai.rootlabs.dev` — `STOCKS_HISTORY_URL` is
applied automatically if empty. They do **not** use Yahoo/TV for history, and do **not** need
`DATABASE_URL`. History API Basic auth is
`WEB_UI_USERNAME` / `WEB_UI_USERNAME` (default `admin`:`admin`), not the
dashboard password. On the VPS set `STOCKS_HISTORY_OWNER=true` and leave
`STOCKS_HISTORY_URL` empty so `--web` still **writes** local Postgres via
`--update-db` and **serves** `GET /api/history`. All OHLCV **reads**
(default `https://33ai.rootlabs.dev`), never local Postgres or CSV.

What both UIs support:

- Explore top TradingView screener symbols and filter the list by ticker.
- Click a symbol to fetch daily or weekly OHLCV history and render a
  TradingView-style candlestick chart.
- Run all registered pattern modules for the selected symbol/timeframe.
  If a pattern is detected, its chart annotations are plotted on the graph
  and the signal appears in the detected-patterns table. Every labeled part
  (first bottom, neckline, second bottom, entry, channel rails, …) prints the
  conditions that part satisfied directly under its label — thresholds are read
  from the detector itself, so the note always matches the live rules.
- **Volume gate** checkbox (toolbar): when checked, detections must also
  clear the RVOL+OBV confirm gate (see [Volume Confirm Gate](#volume-confirm-gate)).
  plus **Compare A/B (Volume)**) runs `Backtester` with the same engine as
  `python main.py --backtest`.
- **Paper Trading**: dual-book desk (US + PH) that runs two `MarketScanner`
  threads, each with its own `PaperAccount` (`core/paper_books.py`). Ledgers
  stay separate (`$` vs `₱`). CLI `python main.py --paper --market us|ph`
  is still one market; dual is `--ui` / `--web` only.
- Download the selected symbol's OHLCV data as CSV / save chart PNG.

and volume-gate code as the scanner. Backtest / Paper Trading are not toy
modes — they call the real `Backtester` / `MarketScanner`.

## Operate via MCP (AI agent)

Full guide to the bot's **MCP (Model Context Protocol) server**. It lets an AI
agent (Command Code, Claude Desktop, or any MCP client) operate and inspect the
bot: explore markets and history, run patterns on a symbol, start/stop and
inspect the US+PH paper books, submit and monitor backtests, run AI pattern
edits, and do DB maintenance.

### How it works

```
AI agent ──stdio (JSON-RPC)──▶ mcp_server ──HTTP + Basic auth──▶ web UI (main.py --web)
                                   │                                 │
                                   │                                 └─ owns paper books,
                                   └─ local calls (db_check/update)     backtest jobs, editor
```

- **Transport is stdio.** The agent spawns `mcp_server` as a child process and
  speaks JSON-RPC over stdin/stdout. The server opens **no port** — nothing to
  expose or firewall, and secrets stay in `.env`.
- **It proxies to the web UI's `/api/*`** (with `WEB_UI_*` Basic auth), so the
  process running `main.py --web` stays the *single owner* of the paper books
  and backtest jobs. The MCP server never starts a second paper/backtest engine.
  **So `python main.py --web` must be running** for anything that touches state.
- Only `db_check`/`db_update` (local Postgres — not in the web API) and
  `deploy_*` (the separate deploy gateway) run outside that proxy.
- **There is no live order path** — `broker/`/`risk/` are not implemented, so
  the tools cover paper, backtest, and the pattern editor only.

### Prerequisites

1. A working checkout with dependencies installed:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
2. A filled-in `.env` (see [Setup](#setup)) — at minimum `WEB_UI_PASSWORD`. The
   MCP server reads the same `.env`.
3. **The web UI running** (the proxy target):
   ```bash
   ./run_web.sh --no-tunnel                 # or: python main.py --web
   curl -fsS http://127.0.0.1:8080/health   # {"ok":true}
   ```
4. An MCP-capable client (Command Code, Claude Desktop, Cursor, …).

### Start the server

Normally the **client spawns the server** — you don't run it by hand. To run or
inspect it directly:

```bash
cd /path/to/trading
./run_mcp.sh                 # cd's to repo root, then execs .venv python -m mcp_server
# or, from the repo root only:
.venv/bin/python -m mcp_server
.venv/bin/python main.py --mcp
```

`run_mcp.sh` is the robust option: it `cd`s to the repo root first, so `.env`
and repo-relative paths resolve regardless of the caller's working directory.
Plain `python -m mcp_server` must be launched **from the repo root** (or with
`PYTHONPATH` pointing at it). Both are silent on stdout — stdout carries the
MCP protocol, so the server must never print to it.

Inspect tools/resources/prompts interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector ./run_mcp.sh
```

### Add it to your MCP client

**Command Code** — add it as a stdio server:

```bash
# from the repo root; prefer the launcher so the working directory is always right
cmd mcp add trading-bot -- /path/to/trading/run_mcp.sh

# or point straight at the venv interpreter (run this from the repo root)
cmd mcp add trading-bot -- /path/to/trading/.venv/bin/python -m mcp_server
```

`--scope` controls where it is stored: `local` (default,
`~/.commandcode/projects/<slug>/mcp.json`), `project` (`.mcp.json`, versioned),
or `user` (`~/.commandcode/mcp.json`). The tools then appear as
`mcp__trading-bot__<tool>`. Verify with `cmd mcp list`, `cmd mcp get trading-bot`,
or the in-session `/mcp` menu.

**Claude Desktop / generic `.mcp.json`** (the launcher keeps `cwd` correct):

```json
{
  "mcpServers": {
    "trading-bot": {
      "command": "/path/to/trading/run_mcp.sh",
      "args": []
    }
  }
}
```

Clients that support an explicit working directory can call the interpreter
directly instead:

```json
{
  "mcpServers": {
    "trading-bot": {
      "command": "/path/to/trading/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/trading"
    }
  }
}
```

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `MCP_API_URL` | `http://127.0.0.1:8080` | Web UI base URL the tools proxy to |
| `MCP_USERNAME` / `MCP_PASSWORD` | falls back to `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` | Basic auth for `/api/*` |
| `MCP_TIMEOUT_SECONDS` | `60` | HTTP timeout for proxied calls |
| `MCP_MAX_HISTORY_BARS` | `2000` | Cap for `get_history_bars`, to bound output |
| `MCP_ALLOW_DESTRUCTIVE` | `false` | Enable paper reset/reset-logs, pattern default/archive, replay clear/upload |
| `MCP_ALLOW_MAINTENANCE` | `false` | Enable `db_check` / `db_update` |
| `MCP_ALLOW_DEPLOY` | `false` | Enable `deploy_status` / `deploy` |
| `MCP_DEPLOY_URL` | `http://127.0.0.1:8099` | Deploy gateway base URL |
| `MCP_DEPLOY_TOKEN` | *(empty)* | Bearer token == the gateway's `DEPLOY_GATEWAY_TOKEN` |

A gated tool returns a clear "disabled — set `MCP_ALLOW_*`" message until its
flag is set, so an agent cannot wipe a ledger or deploy by accident. Flags are
read at process start — restart the client session after editing `.env`.

### Tool reference

⚠ = destructive/maintenance/deploy; requires the matching `MCP_ALLOW_*` flag.

**Market data & explorer**

| Tool | Key parameters | Returns |
|---|---|---|
| `server_health` | — | `{ok, ...}`; never raises — use it to check the web UI is up |
| `get_markets` | — | market profiles (us, ph) + default |
| `list_symbols` | `n=50`, `market` | top-N screener universe |
| `get_history_symbols` | `market` | symbols in the OHLCV store (+ last bar) |
| `get_history_bars` | `symbol`, `market`, `after_ts`, `limit` | daily bars (`limit` capped by `MCP_MAX_HISTORY_BARS`) |
| `get_history_meta` | `symbol`, `market` | row count / first / last bar |
| `analyze_symbol` | `symbol`, `exchange`, `timeframe="1d"`, `run_patterns`, `indicators_only`, `volume_gate`, `kronos_gate`, `kronos_batch`, `market`, `save_chart` | candles + pattern signals + chart PNG (`save_chart` writes a file and returns `chart_png_path`) |

**Pattern registry & editor**

| Tool | Key parameters |
|---|---|
| `list_patterns` | — |
| `get_patterns_pinned` / `get_patterns_bundle` | `selected` (`{pattern_id: version_id}`), `disabled` |
| `list_pattern_versions` | `pattern_id`, `include_archived` |
| `get_pattern_version` / `get_pattern_version_source` / `get_pattern_version_bundle` | `version_id` |
| `diff_pattern_version` | `version_id`, `base_version_id` |
| `get_pattern_provider` | — (AI provider config + balance) |
| `submit_pattern_edit` | `pattern_id`, `base_version_id`, `instruction`, `settings`, `preset_id`/`preset_name`, `chart_context`, `idempotency_key` → `job_id` |
| `get_pattern_edit_job` / `cancel_pattern_edit` / `retry_pattern_edit` | `job_id` |
| `backtest_pattern_version` | `version_id`, `settings` |
| `get_pattern_run` / `get_pattern_run_chart` | `run_id` (, `detection`) |
| ⚠ `set_pattern_default` | `version_id`, `expected_generation`, `idempotency_key` |
| ⚠ `archive_pattern_version` | `version_id`, `expected_generation`, `replacement_default_version_id`, `idempotency_key` |

**Backtests**

| Tool | Key parameters |
|---|---|
| `backtest_catalog` / `backtest_versions` | — / `pattern_id`, `include_archived` |
| `list_backtest_presets` / `save_backtest_preset` | — / `name`, `settings`, `preset_id`, `expected_generation` |
| `submit_backtest_run` | `mode`, `market`, `symbols`, `universe`, `versions`, `start_date`, `end_date`, `session_count`, `warmup_bars`, `initial_capital`, `sizing_mode`, `position_notional`, `txn_cost_pct`, `slippage_pct`, `pattern_only`, `volume_gate`, `kronos_gate`, `kronos_rank`, `collect_first`, `end_policy`, `preset_id`/`preset_name`, `idempotency_key` → `run_id` |
| `get_backtest_run` | `run_id` (payload / state / progress) |
| `cancel_backtest_run` / `retry_backtest_run` | `run_id` |
| `backtest_quick_status` / `backtest_quick_run` | — / `pattern`, `market`, `universe`, `symbols`, `txn_cost_pct`, `volume_gate`, `pattern_only` (single-slot; HTTP 409 if busy) |

**Paper trading**

| Tool | Key parameters |
|---|---|
| `paper_status` | `market` (`us`/`ph`; omitted = both), `lamps` |
| `paper_start` | `market` (required), `n_symbols`, `extra_symbols`, `use_stream`, `stream_start`, `pattern_only`, `indicators_only`, `collect_first`, `collect_first_top_n`, `kronos_gate`/`kronos_rank`/`kronos_batch`, `volume_gate` |
| `paper_start_both` | `us`, `ph` (PaperStartRequest dicts, minus `market`) |
| `paper_stop` | `market` (`us`/`ph`/`all`) |
| ⚠ `paper_reset` | `market` |
| ⚠ `paper_reset_logs` | `market` |
| `paper_export_trades` | `market` |
| `paper_trade_chart` | `market`, `side` (`open`/`closed`), `symbol`, `index`, `log_time`, `trade_id` |

**Kronos · Replay · Maintenance · Deploy**

| Tool | Key parameters | Notes |
|---|---|---|
| `kronos_predict` | `symbol`, `days=3`, `market` | HTTP 404 unless `ENABLE_KRONOS=true` |
| `replay_load` / `replay_chart` | — / chart fields | read-only |
| ⚠ `replay_upload` / `replay_clear` | `books` / — | mutates the stored replay |
| ⚠ `db_check` | `stale_days=7`, `market` | → `{exit_code, report}`; needs local Postgres |
| ⚠ `db_update` | `fetch=True`, `fetch_limit` | writes Postgres |
| ⚠ `deploy_status` / `deploy` | — / `ref`, `commit`, `force` | needs `MCP_DEPLOY_TOKEN` |

**Resources:** `trading://markets`, `trading://patterns/catalog`,
`trading://paper/status`.
**Prompt:** `daily_review(market="us")` — summarizes the paper book and suggests a
next action.

### Example requests

Once connected, just ask the agent in natural language:

- "Which markets can I trade, and which is the default?"
- "Show me the top 20 PH symbols."
- "Analyze AAPL on the daily with the volume gate on, and save the chart."
- "Are the paper books running? Show open positions and R-multiples for the US book."
- "Start the US paper book on the top 50 names with collect-first top 4."
- "Stop all paper books."
- "Run an offline backtest of double_bottom on the top 100 names; report win rate and profit factor."
- "Draft an AI edit to make pattern_003_double_bottom require a first-bar reclaim, then show me the job result."
- "Is the history DB fresh? Check the last 7 days." *(needs `MCP_ALLOW_MAINTENANCE`)*

### Safety & guardrails

- **No live trading.** No tool can place a real order; execution is paper/backtest only.
- **Default-deny destructive ops.** `paper_reset`, `paper_reset_logs`, pattern
  `set_default`/`archive`, `replay_clear`/`upload`, `db_update`, and `deploy`
  each need their `MCP_ALLOW_*` flag in `.env`.
- **No secrets in errors**, and large outputs are bounded (`get_history_bars`
  cap; `analyze_symbol` can write the chart to a file instead of inline base64).
- **One state owner.** The server never runs its own paper/backtest engine, so it
  cannot corrupt a running book's ledger.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "web service unreachable" | `python main.py --web` isn't running, or `MCP_API_URL` is wrong — check `/health`. |
| HTTP 401 from a tool | Bad credentials — set `MCP_USERNAME`/`MCP_PASSWORD` (or `WEB_UI_*`) to the web UI login. |
| Tools don't appear in the client | Reconnect; `cmd mcp list` / `/mcp` (Command Code); confirm the command path is absolute and executable. |
| `No module named mcp_server` | Launch from the repo root, or use `./run_mcp.sh`. |
| HTTP 404 on `kronos_predict` | `ENABLE_KRONOS=false` (Kronos controls hidden by design). |
| HTTP 503 on history/backtest/pattern tools | Postgres (pattern editor / history) unavailable on the web host. |
| HTTP 409 on `paper_start` / `backtest_quick_run` | That book is already running / a quick backtest is already in flight. |
| "Destructive/Maintenance/Deploy MCP tools are disabled" | Set the matching `MCP_ALLOW_*` flag in `.env` and restart the client session. |

## Run the agent on a local model — Hermes + FreeToken

The MCP server above is client-agnostic: any MCP client can drive the bot. This
section wires up [Nous Research **Hermes Agent**](https://hermes-agent.nousresearch.com/)
— a self-hosted agent with persistent memory, auto-skills, subagents, a scheduler,
and a messaging gateway — running entirely on a **local** model served by
[**FreeToken**](https://github.com/FlashML-org/FreeToken)
(`Qwen/Qwen3.6-35B-A3B-FP8`), with **no cloud API key in the agent loop**. It also
makes the whole stack (model server + web UI + agent) come back by itself after a
reboot.

> Full deep-dive, including the exact verified commands and every gotcha:
> [`docs/hermes-integration.md`](docs/hermes-integration.md).

### Two layers — model client vs. bot operator

It is easy to conflate these; they are configured separately:

1. **Hermes as a model client** — Hermes needs an LLM. That LLM is FreeToken's local
   endpoint (`http://127.0.0.1:1919/v1`), which speaks **both** the OpenAI and
   Anthropic APIs. FreeToken ships `ft launch hermes`, which writes Hermes' provider
   config for you.
2. **Hermes as an operator of the bot** — Hermes drives this repo's MCP server
   (`mcp_server/`, stdio) exactly like any other MCP client (see
   [Operate via MCP](#operate-via-mcp-ai-agent)), plus its own shell/file tools.

Neither replaces the app's *internal* AI (Claude vision, DeepSeek pattern editor) —
see [Relationship to the in-app models](#relationship-to-the-in-app-models).

```
┌───────────────────────────┐
│  FreeToken   ft serve     │  OpenAI + Anthropic compatible
│  127.0.0.1:1919           │  /v1/chat/completions, /v1/models
└─────────────┬─────────────┘
              │  model.base_url (OpenAI wire)
┌─────────────▼─────────────┐
│  Hermes Agent             │  memory · skills · scheduler · subagents
│  ~/.hermes/config.yaml    │  terminal + file tools
└───┬───────────────────┬───┘
    │ stdio MCP          │ shell / filesystem / git
    │ (JSON-RPC)         │
┌───▼──────────────┐  ┌──▼───────────────────────┐
│  mcp_server/     │  │  python main.py --web     │
│  (run_mcp.sh)    │─▶│  single state owner      │
└──────────────────┘  └──────────────────────────┘
        proxies /api/* over HTTP + Basic auth
```

Two hard prerequisites, same as the MCP section:

- **FreeToken must be serving the model** — `curl 127.0.0.1:1919/v1/models` returns it.
- **The web UI must be running** — it is the sole owner of the paper books and
  backtest jobs the tools mutate. `curl 127.0.0.1:8080/health` → `{"ok":true}`.

### Prerequisites

| Need | Check / install |
|---|---|
| Hermes installed | `hermes doctor`; install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash --non-interactive` |
| FreeToken installed | `ft --version` |
| A GPU that can hold the model | `nvidia-smi -L` — a 16 GB card runs the 35B-A3B MoE with CPU expert offload |
| Repo venv + deps | `.venv/bin/python -c "import mcp_server"` from the repo root |
| Filled `.env` (min `WEB_UI_PASSWORD`) | `python main.py --web` starts cleanly |
| Web UI running | `curl -fsS http://127.0.0.1:8080/health` → `{"ok":true}` |

### Step 1 — Serve the model locally (FreeToken)

```bash
# dtype / attention / MoE backend / tool-call + reasoning parsers resolve
# automatically from the checkpoint and the GPU.
ft serve --model Qwen/Qwen3.6-35B-A3B-FP8
# → wait for: "API server is ready to serve on 127.0.0.1:1919"

curl -s http://127.0.0.1:1919/v1/models    # exact served model id
curl -s http://127.0.0.1:1919/v1/stats     # ctx, throughput, input_modalities
```

#### ⚠ The KV-cache gotcha (the one that actually bites)

On a 16 GB card the engine's **auto** sizing favors a large MoE expert cache and gives
the KV pool only **~8,000 tokens** — far below Hermes' **64K agent minimum**. Hermes
will still *start* (it reads the model's advertised 256K window) and then fail at
runtime as soon as the conversation grows past the real KV pool.

Pin the split explicitly, sized to the card's cache budget (~7.8 GiB on a 16 GB card):

```bash
ft serve --model Qwen/Qwen3.6-35B-A3B-FP8 \
  --num-tokens 131072 \
  --moe-cache-size 1200
```

- `--num-tokens N` pins the KV pool. KV is ~20 KB/token, and the GPU also holds the
  attention weights, the MoE expert cache, and Mamba state.
- **`--num-tokens` alone OOMs**: it pins KV *without* shrinking the expert cache.
  Always pair it with a smaller `--moe-cache-size`.
- KV is cheap relative to experts, so trading expert slots for context is a good deal —
  e.g. 128K KV + 1200 slots (~7.4 GiB) vs. auto's 8K KV + 2607 slots (~7.8 GiB).
- You can also resize a *running* server, budget-aware, with no restart:
  `ft ctl cache --kv 131072 --moe 1200`. It returns **HTTP 503** and keeps the old
  cache if the request does not fit — use it to find a working split, then bake it into
  the startup flags. `ft ctl cache` reports the live `kv`/`moe`/`mamba` table.

#### Verify tool calling

FreeToken auto-infers a tool-call parser per model family (`qwen3_coder` here) and a
reasoning parser (`qwen3`). Sanity-check before trusting multi-step loops:

```bash
curl -s http://127.0.0.1:1919/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen3.6-35B-A3B-FP8",
  "messages": [{"role":"user","content":"Weather in Tokyo? Use the tool."}],
  "tools": [{"type":"function","function":{"name":"get_weather",
             "parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}],
  "max_tokens": 800 }'
# → choices[0].message.tool_calls should be populated (finish_reason "tool_calls").
```

If instead you get raw JSON printed as `content`, the parser is off — set
`ft serve --tool-call-parser <name>` explicitly and restart.

#### Vision

`GET /v1/stats` reports `model.input_modalities` (`["text","image"]` when a vision
encoder is registered). Hermes does not know this model from models.dev, so set
`model.supports_vision: true` (below) or it will *pre-describe* images instead of
sending them natively — and the chart PNGs this repo renders
(`analyze_symbol(save_chart=true)`, `paper_trade_chart`, `get_pattern_run_chart`) go
unread.

#### Reasoning output

Qwen's thinking lands in `reasoning_content`, separate from the visible `content`.
With a tiny `max_tokens` the whole budget can be spent on reasoning and `content`
comes back **empty** — a budget artifact, not a failure. FreeToken's default output
budget (32,768) is ample, and Hermes does not cap output for custom endpoints.

### Step 2 — Install Hermes and point it at the model

```bash
hermes --version            # confirm the CLI is on PATH (~/.local/bin)
ft launch hermes            # discovers the model, writes Hermes' config, launches it
ft launch hermes --dry-run  # preview the changes first; -y auto-approves
```

`ft launch` reads `/v1/models`, writes the provider config, installs the Hermes CLI if
missing, and **clears cloud keys** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) out of the
child environment so Hermes cannot silently fall back to a paid endpoint.

The equivalent manual config (`~/.hermes/config.yaml`):

```yaml
model:
  provider: custom
  default: Qwen3.6-35B-A3B-FP8            # id from /v1/models
  base_url: http://127.0.0.1:1919/v1
  context_length: 131072                  # match the KV pool from Step 1 (≥ 65536)
  supports_vision: true                    # FreeToken serves image input
```

> **Context length is a contract, not a wish.** Set `model.context_length` to the KV
> pool you actually allocated. Hermes compresses history before that limit; if it is
> set higher than the server can hold, long turns fail at runtime. `hermes config set
> model.context_length 131072` edits it in place.

### Step 3 — Register the trading MCP server

`mcp_server/` is **stdio**, so Hermes spawns it as a child process — no port, no
firewall, secrets stay in `.env`:

```bash
hermes mcp add trading-bot --command /path/to/trading/run_mcp.sh
hermes mcp test  trading-bot          # connects, lists tools, non-zero exit on failure
hermes mcp list                       # show configured servers + status
hermes mcp configure trading-bot      # toggle which tools are exposed
```

Or write it into `~/.hermes/config.yaml` directly:

```yaml
mcp_servers:
  trading-bot:
    command: "/path/to/trading/run_mcp.sh"   # cds to the repo root first
    args: []
```

The discovered tools appear in the model's toolset as **`mcp_trading-bot_<tool>`**
(e.g. `mcp_trading-bot_server_health`, `mcp_trading-bot_paper_status`). Reload a live
session with `/reload-mcp`. The full 52-tool surface (market, patterns, backtest, paper,
kronos, replay, maintenance, deploy) is the same one documented in
[Tool reference](#tool-reference).

To shrink the surface for a financial system, add an allowlist under the server:

```yaml
mcp_servers:
  trading-bot:
    command: "/path/to/trading/run_mcp.sh"
    tools:
      include: [server_health, get_markets, list_symbols, analyze_symbol,
                paper_status, paper_start, paper_stop, submit_backtest_run,
                get_backtest_run]
```

### Step 4 — Make it survive reboots

The bot's web UI already ships as a systemd **user** service with `Restart=always`
(see [Keep it running across restarts](#keep-it-running-across-restarts-systemd)), and
user-systemd **linger** is enabled, so user services start at boot with no login. Give
the model server the same treatment — the unit mirrors `deploy/trading-web.service`:

```ini
# ~/.config/systemd/user/freetoken.service
[Unit]
Description=FreeToken model server (Qwen3.6-35B-A3B-FP8)
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=%h
# --num-tokens + --moe-cache-size pin a KV pool big enough for the agent (see Step 1).
ExecStart=%h/.freetoken/venv/bin/ft serve --model %h/.freetoken/models/Qwen3.6-35B-A3B-FP8 \
  --host 127.0.0.1 --port 1919 --moe-backend auto --max-running-requests 4 \
  --memory-ratio 0.85 --num-tokens 131072 --moe-cache-size 1200
Restart=always
RestartSec=10
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

Install the Hermes **gateway** (its scheduler + messaging surfaces) as a user service:

```bash
hermes gateway install     # writes + enables ~/.config/systemd/user/hermes-gateway.service
```

Order it after its dependencies so it does not come up before the model server and the
web UI:

```ini
# ~/.config/systemd/user/hermes-gateway.service.d/after-stack.conf
[Unit]
After=freetoken.service trading-web.service
Wants=freetoken.service trading-web.service
```

Then enable and confirm the boot set:

```bash
systemctl --user daemon-reload
systemctl --user enable --now freetoken.service
systemctl --user list-dependencies default.target      # all units pulled in at boot
systemctl --user is-enabled freetoken hermes-gateway trading-web
loginctl show-user "$USER" | grep Linger                # → Linger=yes
```

> **FreeToken Desktop conflict.** The Desktop app launches its *own* `ft serve` on port
> 1919. If the systemd unit owns the port, don't also start the model from the Desktop
> app. Killing the app alone is **not** enough — its worker children reparent to
> `systemd --user` and keep port **1920** (the torch rendezvous store) *and* the GPU,
> producing an `EADDRINUSE` crash loop. Reap the leftover `ft serve` / `spawn_main`
> pids before starting the service.

### Operations

```bash
systemctl --user status  freetoken hermes-gateway trading-web   # states
systemctl --user restart freetoken.service                      # after changing flags
journalctl --user -u freetoken -f                               # model-server log
journalctl --user -u hermes-gateway -f                          # gateway log
curl -s http://127.0.0.1:1919/v1/cache/status                   # KV / expert geometry
curl -s http://127.0.0.1:1919/v1/stats                          # throughput, modalities
hermes status                                                   # model, gateway, jobs
hermes -z "<prompt>"                                            # one-shot, headless
```

A quick end-to-end check — ask the local model to use the tools:

```bash
hermes -z "Use the trading-bot MCP tools: call server_health, then list_symbols for the us market."
```

The MCP server also exposes a `daily_review(market="us")` prompt (paper-book summary +
one suggested next action) — a natural cron job to run through the gateway.

### Relationship to the in-app models

Hermes' model and the app's internal AI are **independent**:

| Caller | Model | Configured in |
|---|---|---|
| Hermes (the agent) | Qwen3.6-35B-A3B-FP8 via FreeToken | `~/.hermes/config.yaml` |
| Chart vision confirmation | Anthropic Claude | repo `.env` (`VISION_CONFIRMATION_ENABLED`, `ANTHROPIC_*`) |
| AI pattern editing | DeepSeek | repo `.env` (`DEEPSEEK_*`) |

So `submit_pattern_edit` still spends DeepSeek credits and the vision gate still calls
Claude, regardless of Hermes running on a local model. Making *those* local is separate
work — the pattern-editor provider is a single adapter (`ai/providers/deepseek.py`) and
vision is `analysis/vision_checker.py`.

### Configuration reference

**Hermes side** (`~/.hermes/config.yaml` + `~/.hermes/.env`):

| Key | Meaning |
|---|---|
| `model.provider: custom` | Any OpenAI-compatible endpoint (FreeToken) |
| `model.base_url` | `http://127.0.0.1:1919/v1` |
| `model.default` | served model id from `/v1/models` |
| `model.context_length` | pin to the allocated KV pool (e.g. `131072`; ≥ `65536`) |
| `model.supports_vision` | `true` — FreeToken serves image input |
| `mcp_servers.trading-bot.*` | stdio MCP server via `run_mcp.sh` |
| `auxiliary.*` | route compression/vision to another model if the local 35B is weak there |

**Repo side** — unchanged; the MCP server reads the same `.env` documented in
[Configuration (`.env`)](#configuration-env). The relevant knobs are the `MCP_*` group,
especially the default-deny `MCP_ALLOW_DESTRUCTIVE` / `MCP_ALLOW_MAINTENANCE` /
`MCP_ALLOW_DEPLOY` flags.

### Safety

- **No live trading.** Broker/risk are unimplemented — paper and backtest only.
- **Default-deny mutations.** Every destructive tool needs its `MCP_ALLOW_*` flag,
  regardless of what the agent tries to call.
- **Allowlist the MCP surface** (`tools.include`) for a financial system and add tools
  deliberately.
- **One state owner.** The MCP server never starts a second engine, so it cannot corrupt
  a running book's ledger.
- **Unattended runs need care.** Hermes can route tool-call approvals to the CLI or a
  messaging platform. Decide which actions may auto-proceed *before* running headless —
  `--yolo` disables approval entirely, which is unsafe for state-mutating tools.
- **Secrets** stay in the repo `.env`; the MCP server opens no network port.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `EADDRINUSE` on port **1920**, service crash-loops | Orphaned FreeToken children (from a killed Desktop app) hold the torch rendezvous port/GPU. Reap the leftover `ft serve` / `spawn_main` pids, then restart. |
| CUDA OOM on startup after adding `--num-tokens` | KV pinned without shrinking the expert cache — add `--moe-cache-size` (Step 1). |
| `HTTP 503` from `ft ctl cache` | Requested cache exceeds the budget; lower `--kv`/`--moe`. The old cache is kept. |
| Hermes refuses to start: "context limit" | Local window < 64K — allocate ≥ 64K KV and pin `model.context_length` ≥ `65536`. |
| Model prints tool calls as text | FreeToken tool-call parser off — set `--tool-call-parser` and restart. |
| `mcp_trading-bot_*` tools missing | `hermes mcp test trading-bot`; confirm `run_mcp.sh` is absolute + executable; check no `tools.include` filtered them; `/reload-mcp`. |
| "web service unreachable" | `python main.py --web` not running, or `MCP_API_URL` wrong — check `/health`. |
| HTTP 401 from a tool | Bad `MCP_*` / `WEB_UI_*` credentials. |
| Empty assistant `content`, answer in reasoning | Tiny `max_tokens` consumed by thinking — give output budget (FreeToken default 32K). |
| Hermes won't see chart images | Set `model.supports_vision: true`. |
| `ft launch hermes` fell back to a paid endpoint | It clears cloud keys from the child env; check the config it wrote still points at `127.0.0.1:1919`. |

## Deploying to the engine from another computer — optional, host-specific

`deploy_to_engine.sh` deploys this repo to the machine that runs the deploy
gateway (the engine serving the public UI) from any computer — no SSH key
distribution.

The client pushes the branch to GitHub, then POSTs the ref to an authenticated
webhook (`web/deploy_gateway.py`). The gateway fetches that exact commit, checks
it out, syncs deps and restarts `trading-web.service`. It runs as its own user
service behind its own Cloudflare Tunnel, so a deploy (which restarts the web
UI) never takes the deploy endpoint down. See `deploy_to_contabo.sh` for the
separate VPS deploy.

This is inert unless the host sets `DEPLOY_GATEWAY_TOKEN` in `.env`: the gateway
and `run_deploy.sh` both refuse to start without a token, so a plain clone never
runs it.

### One-time setup (on the engine)
```bash
# 1. Create the tunnel + DNS and print the tokens. DEPLOY_TUNNEL_NAME and
#    DEPLOY_TUNNEL_HOSTNAME are required (env or .env); the zone is derived from
#    the hostname unless DEPLOY_ZONE is set.
DEPLOY_TUNNEL_NAME=<tunnel-name> DEPLOY_TUNNEL_HOSTNAME=<deploy-host> \
  ./scripts/setup_deploy_tunnel.sh

# 2. Put the printed values in .env
DEPLOY_GATEWAY_TOKEN=<generated>
DEPLOY_TUNNEL_HOSTNAME=<deploy-host>
DEPLOY_TUNNEL_TOKEN=<tunnel token>

# 3. Install the always-on user service (survives reboots)
./scripts/install_deploy_service.sh
curl -fsS https://<deploy-host>/health
```

### Deploying (from any computer)
```bash
export DEPLOY_TOKEN=<the engine's DEPLOY_GATEWAY_TOKEN>
export DEPLOY_URL=https://<deploy-host>     # overrides the built-in default
./deploy_to_engine.sh                 # push + deploy the current branch
./deploy_to_engine.sh --ref main
./deploy_to_engine.sh --no-push       # deploy an already-pushed commit
```

### Security
This endpoint runs code, so it binds `127.0.0.1` only, requires
`DEPLOY_GATEWAY_TOKEN` (constant-time compare) and refuses to start without it,
only ever runs a fixed command list (never a shell), and refuses to deploy over
a dirty tree unless `--force`. For defense in depth, put a Cloudflare Access
application in front of the deploy host (Zero Trust → Access → allow only your
account).

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
│   ├── indicator_engine.py              # SMA, RSI, MACD, BB, ATR, OBV, VWAP...
│   ├── price_volume.py                  # RVOL + OBV volume confirm gate
│   ├── chart_renderer.py                # mplfinance candlestick chart → PNG
│   └── vision_checker.py               # Claude vision API confirmation
│
├── patterns/
│   ├── base_pattern.py                  # Abstract base — analyze() interface
│   └── 003_double_bottom.py             # Example chart pattern
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
│   ├── paper_books.py                   # Dual-market paper manager (US thread + PH thread)
│   ├── backtester.py                    # Historical walk-forward backtest engine
│   ├── test_volume_gate.py              # Unit tests for analysis.price_volume
│
├── ui/
│   ├── app.py                           # Native tkinter symbol explorer (--ui)
│   └── paper_dashboard.py               # Dual-book Paper desk (US + PH cards)
│
├── web/
│   ├── app.py                           # Authenticated FastAPI web UI (--web)
│   ├── auth.py                          # Session login (WEB_UI_PASSWORD required)
│   ├── services.py                      # Explorer data/pattern/chart helpers
│   └── jobs.py                          # Background backtest; paper delegates to paper_books
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
| Volume confirm | RVOL ≥ `VOLUME_GATE_RVOL_MIN` + OBV slope agrees with BUY/SELL | Weak volume or OBV against the trade | `VOLUME_GATE_ENABLED=true` |
| Vision confirmation | Claude looks at the chart PNG | Pattern not visually present | `VISION_CONFIRMATION_ENABLED=false` |

Then risk_guard / paper sizing add hard limits before any order fires.
Vision runs after the volume gate so rejected volume does not spend Claude tokens.

## Safety Rules
- Always start with `TRADING_MODE=paper`
- Only switch to `live` after Toby signs off on the pattern in paper trading
- All charts saved to `/charts/` for Toby's review
- All trades logged to `logs/trades.log` permanently
