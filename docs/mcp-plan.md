# Trading Bot → MCP Server — Implementation Plan

Status: proposed. Lets an AI agent **operate and inspect the trading bot through an MCP
(Model Context Protocol) server**: explore markets, read OHLCV history, run pattern analysis,
control the US+PH paper books, submit/monitor backtests, drive the AI pattern editor, and run
DB maintenance — all as typed MCP tools.

Decisions locked with the user:
- **Transport:** `stdio`, local (the agent spawns the server in the repo venv — no network listener).
- **Scope:** all four domains — market data/explorer, paper trading, backtests, pattern editor + maintenance.
- **State owner:** **proxy to the running web service** over its authenticated HTTP API, so there is exactly
  one owner of paper books / backtest jobs.

## 1. Goal

Give an agent a single, typed, safe control surface over the bot. The bot already exposes all of this over
its FastAPI web UI; the MCP server is a thin, well-described wrapper so an LLM can drive it without scraping
HTML or shelling out to `main.py`.

## 2. Codebase map (what exists today)

**Entry points** (`main.py`, 999 lines): one `argparse` dispatcher. Modes: live/paper scan (`run_scanner`),
`--paper` (`run_paper`), `--backtest` (`run_backtest`), `--ui` (tkinter), `--web` (FastAPI), `--papertrade-stream`,
`--kronos-test`/`--kronos-finetune`, `--check-db`/`--update-db`, `--ingest/export/import-pse`.
`--ui`/`--web` own their own event loop and are dispatched directly in `__main__`.

**Core engine** (`core/`):
- `scanner.py::MarketScanner` — infinite `async run()` loop; `start()`/`stop()` are sync flag-setters; no self-owned thread.
  Signals → `PaperAccount.open_position()` (or a log-only "IBKR disabled" line). `self.stats` telemetry dict.
- `paper_trader.py::PaperAccount` — RLock-guarded ledger; `load/save`, `snapshot_metrics()`, `positions_snapshot()`,
  `equity()`, `export`. Ledgers: `data/cache/paper_account.json` (US), `data/cache/paper_account_ph.json` (PH).
- `paper_books.py::paper_books` (singleton `PaperBookManager`) — one `PaperBook` (daemon thread + private asyncio
  loop + optional stream subprocess) per market; `start/stop/snapshot/snapshot_all/lamps/reset/reset_logs/chart/export_trades`.
- `backtester.py::Backtester` — async walk-forward; `backtest_service.py::BacktestService` — **durable, Postgres-backed**
  typed façade (`submit`, `execute`, `status`, `progress`, `result`, `cancel`, `retry`, presets, catalog). `run_coro()` helper.
- `signal_log_store.py` — JSONL signal log per market under `logs/paper_signals_{us,ph}.jsonl`.
- `market.py::MarketProfile` — US/PH profiles (currency, tz, lot rounding, long-only, ledger path, scan interval).
- `engine_defaults.py::ENGINE` — flat `$10k` notional, `min_bars=100`.

**Patterns / analysis** (`patterns/`, `analysis/`, `indicators/`):
- `BasePattern.analyze(snapshot, store) -> TradeSignal | None`; `TradeSignal` carries action/qty/stop/tp/annotations.
- Registration via `core/pattern_loader.py::discover()` → `PatternVersions.resolve()` (Postgres catalog `patterns`).
- `patterns/chart_scan.py::latest_signals_over_lookback(...)`; `analysis/chart_renderer.py`, `vision_checker.py`, `price_volume.py`.

**Data layer** (`data/`): `tv_client.py::TVClient` (TradingView screener + **client** to a stdio `tradingview-mcp`
subprocess via the `mcp` SDK), `history_client.py` (HTTP client for `https://33ai.edos.uk` `/api/history`),
`history.py` (unified daily OHLCV facade), `stream_client.py`/`stream_server.py` (local WebSocket replay),
`db.py`/`update.py`/`check.py`/`pse_ingest.py` (Postgres), `universes/`, `barcache.py`.

**Web UI** (`web/app.py`) — FastAPI, the operational surface we will proxy to. Auth: session cookie `tb_session`
or HTTP Basic (`WEB_UI_USERNAME`/`WEB_UI_PASSWORD`). ~24 JSON `/api/*` groups:
- Explorer/history: `/api/markets`, `/api/symbols`, `/api/history/symbols`, `/api/history/bulk`,
  `/api/history/{symbol}/meta`, `/api/history/{symbol}`, `POST /api/symbol` (patterns + chart PNG + csv).
- Backtest: `/api/backtest/status`, `/api/backtest/run`, `/api/backtest/catalog`, `/api/backtest/presets`,
  `/api/backtest/runs` (submit/status/cancel/retry).
- Patterns: `/api/patterns`, `/api/patterns/bundle`, `/api/patterns/{id}/versions`, `/api/patterns/versions/{id}`
  (+ `/source`, `/diff`, `/bundle`, `/backtest`, `/default`, `/archive`), `/api/patterns/edits` (submit/status/cancel/retry),
  `/api/patterns/runs/{id}` (+ `/chart`), `/api/patterns/provider`.
- Paper: `/api/paper/status`, `/start`, `/start-both`, `/stop`, `/reset`, `/reset-logs`, `/chart`, `/export`.
- Kronos: `POST /api/kronos/predict` (404 when `ENABLE_KRONOS=false`).
- Replay: `/api/replay/upload|load|clear|chart`.

**Deploy gateway** (`web/deploy_gateway.py`) — separate FastAPI app, loopback + bearer `DEPLOY_GATEWAY_TOKEN`,
`GET /health`, `POST /deploy`.

**Key gaps that constrain the design:**
- `broker/` and `risk/` are **empty**; IBKR is commented out. **There is no real order path** — paper/backtest only.
  The MCP server therefore cannot and will not place live trades.
- No MCP **server** exists. The `mcp` SDK is present only as a **client** (`data/tv_client.py`) and is pinned
  `mcp>=1.28.0,<2` in `requirements.txt` (its high-level `mcp.server.fastmcp.FastMCP` gives us the server for free).
- Service convention: `deploy/*.service` templates + `scripts/install_*_service.sh` → systemd **user** units (linger).

## 3. Architecture

**One process, stdio transport, HTTP proxy to the web UI.**

```
AI agent (Command Code / Claude Desktop)
   │  stdio (JSON-RPC)
   ▼
mcp_server  ── httpx (Basic auth) ──►  FastAPI web UI (python main.py --web)
   │                                        │ owns paper_books, backtest jobs, pattern editor
   └── local module calls (maintenance) ──► data.check.run_check / data.update.run_update
```

- **Why proxy:** `paper_books` and the in-process backtest job are **process-local state**. If the MCP server
  instantiated its own `PaperBookManager`/`BacktestJob`, two processes would write the same ledger files and
  job slots — a correctness bug. Proxying to the already-running web service (which the bot runs as anyway)
  keeps a single owner and reuses the existing auth + remote-pattern-proxy behavior.
- **Reads also go over HTTP** (universe, history, explorer scan) so there is one access path and consistent
  auth. The exploratory `POST /api/symbol` already returns patterns + chart + CSV.
- **Maintenance (`--update-db`/`--check-db`) is not in the web API**; those are stateless w.r.t. the web process,
  so the MCP server calls `data.check.run_check` / `data.update.run_update` **in-process** (env-gated).
- **No new network surface:** stdio only. Secrets stay in `.env`.

**Naming gotcha (must follow):** do **not** create a top-level package named `mcp/` — the repo root is on
`sys.path`, so it would shadow the installed `mcp` SDK and break `data/tv_client.py`. Use the subpackage name
**`mcp_server/`**.

## 4. New code & files

New package `mcp_server/` (repo root):

| File | Purpose |
|---|---|
| `mcp_server/__init__.py` | package marker |
| `mcp_server/__main__.py` | `python -m mcp_server` → `server.main()` |
| `mcp_server/server.py` | build `FastMCP("trading-bot")`, register tools/resources/prompts, `run()` (stdio) |
| `mcp_server/config.py` | MCP settings helpers reading `config.settings` (see §6) |
| `mcp_server/api_client.py` | `TradingApiClient` — httpx client w/ Basic auth, path builders, error mapping, history caps |
| `mcp_server/tools_market.py` | tool group A |
| `mcp_server/tools_patterns.py` | tool group B |
| `mcp_server/tools_backtest.py` | tool group C |
| `mcp_server/tools_paper.py` | tool group D |
| `mcp_server/tools_kronos.py` | tool group E |
| `mcp_server/tools_replay.py` | tool group F (optional) |
| `mcp_server/tools_maintenance.py` | tool group G (local calls) |
| `mcp_server/tools_deploy.py` | tool group H (optional, token-gated) |
| `mcp_server/test_mcp_server.py` | pytest (offline, ASGI transport) |

Modified files:
- `main.py` — add `--mcp`; in the `__main__` block dispatch `from mcp_server.server import run; run()`
  (alongside `--ui`/`--web`, since FastMCP stdio owns its own loop).
- `config.py` — add MCP settings (see §6).
- `.env.example` — document the new `MCP_*` vars in the existing commented style.
- `README.md` — new "Operate via MCP" section (client config + example calls).
- `requirements.txt` — **no change** (`mcp>=1.28.0,<2` and `httpx` already present).
- `docs/mcp-plan.md` — this document.

Server construction: use the high-level **`FastMCP` from the pinned SDK** (`mcp.server.fastmcp.FastMCP`).
If the exact symbol differs in `1.28`, fall back to the low-level `mcp.server.Server` and register handlers
manually — both ship in the pinned package, so no dependency is added.

## 5. Tool catalog

Tools are thin wrappers that build the request and call the web API (or a local module). Every tool returns
compact JSON; large payloads are truncated or written to a file under `logs/mcp/` and returned as a path.

**A. Market data & explorer** (`ExplorerService`-backed, `require_login`)
| Tool | Backing call |
|---|---|
| `get_markets()` | `GET /api/markets` |
| `list_symbols(n=50, market=None)` | `GET /api/symbols?n&market` |
| `get_history_symbols(market=None)` | `GET /api/history/symbols?market` |
| `get_history_bars(symbol, market=None, after_ts=None, limit=None)` | `GET /api/history/{symbol}` (cap `limit` ≤ 2000) |
| `get_history_meta(symbol, market=None)` | `GET /api/history/{symbol}/meta` |
| `analyze_symbol(symbol, exchange=None, timeframe="1d", run_patterns=True, indicators_only=False, kronos_gate=None, kronos_batch=None, volume_gate=None, market=None, save_chart=None)` | `POST /api/symbol` |

**B. Patterns & editor**
| Tool | Backing call |
|---|---|
| `list_patterns()` | `GET /api/patterns` (fallback `GET /api/backtest/catalog`) |
| `list_pattern_versions(pattern_id, include_archived=False)` | `GET /api/patterns/{pattern_id}/versions` |
| `get_pattern_version(version_id)` | `GET /api/patterns/versions/{version_id}` |
| `get_pattern_version_source(version_id)` | `.../source` |
| `diff_pattern_version(version_id, base_version_id=None)` | `.../diff` |
| `get_patterns_bundle(selected=None, disabled=None)` | `GET /api/patterns/bundle` |
| `get_pattern_provider()` | `GET /api/patterns/provider` |
| `submit_pattern_edit(pattern_id, base_version_id, instruction, settings=None, preset_id/name=None, chart_context=None, idempotency_key=None)` | `POST /api/patterns/edits` → `job_id` |
| `get_pattern_edit_job(job_id)` | `GET /api/patterns/edits/{job_id}` |
| `cancel_pattern_edit(job_id)` / `retry_pattern_edit(job_id)` | `.../cancel` / `.../retry` |
| `backtest_pattern_version(version_id, settings)` | `POST /api/patterns/versions/{version_id}/backtest` |
| `get_pattern_run(run_id)` / `get_pattern_run_chart(run_id, detection)` | `GET /api/patterns/runs/{run_id}` (+ `/chart`) |
| `set_pattern_default(version_id, expected_generation, idempotency_key=None)` ⚠ | `POST .../default` |
| `archive_pattern_version(version_id, expected_generation, replacement_default_version_id=None, idempotency_key=None)` ⚠ | `POST .../archive` |

**C. Backtests**
| Tool | Backing call |
|---|---|
| `backtest_quick_status()` | `GET /api/backtest/status` |
| `backtest_quick_run(...)` | `POST /api/backtest/run` (single-slot in-process job) |
| `backtest_catalog()` | `GET /api/backtest/catalog` |
| `list_backtest_presets()` / `save_backtest_preset(name, settings, preset_id=None, expected_generation=None)` | `GET`/`POST /api/backtest/presets` |
| `submit_backtest_run(request)` | `POST /api/backtest/runs` → `run_id` |
| `get_backtest_run(run_id)` | `GET /api/backtest/runs/{run_id}` |
| `cancel_backtest_run(run_id)` / `retry_backtest_run(run_id)` | `.../cancel` / `.../retry` |

**D. Paper trading**
| Tool | Backing call |
|---|---|
| `paper_status(market=None, lamps=False)` | `GET /api/paper/status` |
| `paper_start(market, n_symbols=50, extra_symbols="", use_stream=False, stream_start=None, pattern_only=False, indicators_only=False, collect_first=False, collect_first_top_n=4, kronos_gate=None, kronos_rank=None, kronos_batch=None, volume_gate=None)` | `POST /api/paper/start` |
| `paper_start_both(us=None, ph=None)` | `POST /api/paper/start-both` |
| `paper_stop(market="all")` | `POST /api/paper/stop` |
| `paper_reset(market)` ⚠ | `POST /api/paper/reset` |
| `paper_reset_logs(market="all")` ⚠ | `POST /api/paper/reset-logs` |
| `paper_export_trades(market="all")` | `GET /api/paper/export` |
| `paper_trade_chart(market, side, symbol=None, index=None, log_time=None, trade_id=None)` | `GET /api/paper/chart` |

**E. Kronos** (404 when `ENABLE_KRONOS=false`): `kronos_predict(symbol, days=3, market=None)` → `POST /api/kronos/predict`.

**F. Replay** (optional): `replay_load()`, `replay_upload(books)` ⚠, `replay_clear()` ⚠, `replay_chart(request)`.

**G. Maintenance** (in-process; gated by `MCP_ALLOW_MAINTENANCE`)
- `db_check(stale_days=7, market=None)` → `data.check.run_check(...)` (return code + stats).
- `db_update(fetch=True, fetch_limit=None)` → `data.update.run_update(...)` ⚠ (heavy; writes Postgres).

**H. Deploy** (optional; gated by `MCP_ALLOW_DEPLOY`, hits the separate gateway with `MCP_DEPLOY_TOKEN`)
- `deploy_status()` → `GET {MCP_DEPLOY_URL}/health`.
- `deploy(ref, commit=None, force=False)` ⚠ → `POST {MCP_DEPLOY_URL}/deploy`.

⚠ = mutating/destructive; refused unless its enabling flag is set (§7).

**Resources:** `trading://markets`, `trading://patterns/catalog`, `trading://paper/status`.
**Prompt:** `daily_review(market="us")` — pull `paper_status` + the latest backtest result and summarize.

## 6. Configuration (new settings)

Add to `config.py::Settings` (env file `.env`; field names map to `MCP_*`):

```python
# ── MCP server (python main.py --mcp / python -m mcp_server) ──────────────
mcp_api_url: str = "http://127.0.0.1:8080"   # the running web UI to proxy
mcp_username: str = ""                       # default: web_ui_username
mcp_password: str = ""                       # default: web_ui_password
mcp_timeout_seconds: float = 60.0
mcp_max_history_bars: int = 2000
mcp_allow_destructive: bool = False          # paper reset/reset-logs, set-default/archive, replay clear/upload
mcp_allow_maintenance: bool = False          # db_check/db_update
mcp_allow_deploy: bool = False               # deploy_status/deploy
mcp_deploy_url: str = "http://127.0.0.1:8099"
mcp_deploy_token: str = ""                   # == DEPLOY_GATEWAY_TOKEN on the gateway host
```

Document each in `.env.example` with the repo's existing comment style (empty/generic defaults so a plain
clone behaves as before — matches the project's host-specific-infra convention).

Auth resolution: `mcp_username/password` fall back to `settings.web_ui_username` / `settings.web_ui_password`
(the same Basic credentials the web UI accepts for `/api/*`).

Client wiring (README): a `.mcp.json` / agent-config entry that runs
`"command": "<repo>/.venv/bin/python", "args": ["-m", "mcp_server"], "cwd": "<repo>"`.
`cwd` must be the repo root so `config.py`, `.env`, and relative paths (`logs/`, `data/cache/`) resolve.

## 7. Security & safety

- **No live trading, ever, from MCP.** There is no order path in the repo today; the server exposes only
  paper/backtest/editor tools and asserts `settings.trading_mode != live` for anything execution-adjacent.
- **stdio only** → no listener, no port, no tunnel; nothing to expose. Secrets remain in `.env`.
- **Default-deny destructive ops.** `paper_reset`, `paper_reset_logs`, `archive`, `set_default`, `replay_clear/upload`,
  `db_update`, and `deploy` each require their `MCP_ALLOW_*` flag; otherwise the tool returns a clear refusal.
- **Never echo secrets.** Error responses surface only HTTP status + safe message (mirror `web/patterns.py::patterns_error`).
- **Bounded outputs.** Cap `get_history_bars` `limit`; truncate `analyze_symbol` (return `chart_png_b64`, or write
  to `logs/mcp/` and return the path); summarize large backtest payloads.
- **Preserve idempotency.** Pass through `idempotency_key` / `expected_generation` on editor + run tools so
  retries are safe.

## 8. Testing

`mcp_server/test_mcp_server.py` (pytest), **offline** — no network, no live web server:
- Build `web.app.create_app()` and drive it with **httpx `ASGITransport`**; point `TradingApiClient` at it.
  (Reuses the FastAPI app already in-repo, like `web/test_http_auth.py`.)
- Assertions:
  - Tool inventory matches expected names (all groups registered).
  - `get_markets` / `list_symbols` map to the right path + query and parse the response.
  - `paper_start` serializes the `PaperStartRequest` shape correctly.
  - `paper_status` handles the single-book vs `snapshot_all` shapes.
  - Destructive tool **refuses** with the flag off and **calls through** with it on.
  - Error mapping: 401 (bad creds), 409 (job busy / already running), 503 (DB down) → typed tool errors.
  - `get_history_bars` clamps `limit` to `mcp_max_history_bars`.
- Maintenance tools: monkeypatch `data.check.run_check` / `data.update.run_update`; assert gating and arg pass-through.
- Run alongside existing suites: `./.venv/bin/python -m pytest mcp_server -q`.

## 9. Verification (end-to-end)

1. **Web UI up** (required — the MCP server proxies to it):
   `WEB_UI_PASSWORD=... ./.venv/bin/python main.py --web` (or `./run_web.sh --no-tunnel`); confirm `curl -fsS localhost:8080/health`.
2. **Server lists tools over stdio:**
   `./.venv/bin/python -m mcp_server` starts and speaks MCP; inspect with the MCP Inspector
   (`npx @modelcontextprotocol/inspector ./.venv/bin/python -m mcp_server`) — verify every tool group is present.
3. **Wire the agent** via `.mcp.json` (command `<repo>/.venv/bin/python`, args `-m mcp_server`, cwd `<repo>`),
   restart the agent, confirm the `trading-bot` server connects.
4. **Call tools:** `get_markets` → `list_symbols` → `analyze_symbol` (returns signals + chart) →
   `paper_status` → `paper_start(us)` → poll `paper_status` → `paper_stop`.
5. **Backtests:** `backtest_catalog` → `submit_backtest_run` → poll `get_backtest_run` → `cancel_backtest_run`.
6. **Editor:** `submit_pattern_edit` → `get_pattern_edit_job`; confirm `set_pattern_default` is refused with
   `MCP_ALLOW_DESTRUCTIVE=false`, then allowed when flipped.
7. **Regression:** `./.venv/bin/python -m pytest mcp_server web core -q`.

## 10. Out of scope / notes

- **Live order placement is not in scope** (no broker/risk code exists). If real execution is later added
  (`broker/`, `risk/`), the MCP surface would grow behind a hard live-mode gate — not part of this plan.
- **`--ui` (tkinter) is not exposed** — no reason to drive a desktop GUI over MCP.
- **HTTP transport** was explicitly deferred (user chose stdio). If a remote agent later needs it, add an
  env-gated streamable-HTTP mode + token + `deploy/trading-mcp.service`, mirroring the deploy gateway.
- The web UI must be running for stateful tools. If it is down, tools return a clear "web service unreachable"
  error rather than falling back to a second in-process owner.
