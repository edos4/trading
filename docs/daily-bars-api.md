# Daily bars from `stocks_history`

How this repo reads daily OHLCV. One PostgreSQL database, `stocks_history`,
lives on the VPS. Everyone else reads it through the Python HTTP API that
the VPS web app serves. Readers do not use CSV files.

## The database

`stocks_history` is local PostgreSQL on the history owner (Contabo /
`33ai.rootlabs.dev`). The app connects over the unix socket with peer auth
(`DATABASE_URL`, default
`postgresql://r00t@/stocks_history?host=/var/run/postgresql`). Schema is
created by `data/db.py`.

| Table | Key | What it holds |
|---|---|---|
| `daily_bars` | `(symbol, ts)` | One OHLCV row. `ts` is Unix seconds and is the row identity. `bar_date` is derived in the writer: `(to_timestamp(ts) AT TIME ZONE tz)::date`, America/New_York for US and Asia/Manila for PH. |
| `hourly_bars` | `(symbol, ts)` | Same columns, filled by `--update-hourly` from Alpaca. No HTTP route. |
| `symbols` | `symbol` | `market` (`us` or `ph`), `last_bar_ts`, `row_count`, `last_hourly_bar_ts`. |

PH tickers are stored with a `.PS` suffix. `core.market.ph_history_symbol`
maps the bot ticker `BDO` to the storage key `BDO.PS` and leaves an existing
`.PS` alone. US tickers are stored as-is.

Writes stay on the owner: `python main.py --update-db` / `--check-db`
(Yahoo and PSE Edge into `daily_bars`), `--update-hourly` (Alpaca into
`hourly_bars`), and `--ingest-pse`. Ingest never calls `GET /api/history`
on itself.

## Who reads which way

`data/history.py` is the facade. `local_history_backfill_enabled()` is the
switch: true only when `STOCKS_HISTORY_URL` is empty **and** this process
owns the database.

A process owns the database when `STOCKS_HISTORY_OWNER=true`, when the
working directory is `/home/deploy/apps/trading`, or when the hostname
contains `33ai.rootlabs.dev` (`owns_local_stocks_history()`).

| Process | Read path |
|---|---|
| VPS `--web` with owner set and URL empty | `data/db.py` against local Postgres, and this process **serves** `/api/history` |
| Laptop `--web`, scanner, paper stream, backtest, MCP | `data/history_client.py` → `https://33ai.rootlabs.dev` |

`python main.py --web` calls `enable_ui_web_history()`. On a laptop with an
empty URL that sets `STOCKS_HISTORY_URL` to `https://33ai.rootlabs.dev` and
does not fall back to Yahoo or TradingView. On the owner it leaves the URL
empty and keeps the Postgres path.

`--web` charts and OHLCV never use Yahoo. TradingView is only a fallback
outside `--web`, from `fetch_ohlcv_candles(..., tv_fallback=True)`.

## Python API

Served by the FastAPI app in `web/app.py` (port 8080). Every route depends
on `require_history`.

Auth is a session cookie **or** HTTP Basic where the username and the
password are both `WEB_UI_USERNAME` (default `admin` / `admin`). That is
not the dashboard password. `web.auth.verify_history_credentials` checks
both sides against `WEB_UI_USERNAME`. A 401 body is
`{"detail": "Not authenticated"}`.

`market` is `us` or `ph` (optional). Anything else on the symbol list is
400. A missing symbol or an empty bar range is 404
`{"detail": "No daily bars for {symbol}."}`. Postgres down is 503
`{"detail": "History database unavailable."}`.

### `GET /api/history/symbols`

Query: `market=us|ph` (optional).

```json
{"symbols": [{"symbol": "AAPL", "market": "us", "last_bar_ts": 1794499200, "row_count": 4552}]}
```

Backed by `data.db.all_symbols` (`SELECT … FROM symbols [WHERE market = %s] ORDER BY symbol`).

### `GET /api/history/{symbol}`

Query: `after_ts` (sessions with `ts` strictly greater), `limit` (newest N
rows, then returned oldest-first), `market`.

```json
{
  "symbol": "BDO.PS",
  "bars": [
    {"ts": 1794499200, "date": "2026-11-13", "open": 320.0, "high": 326.0,
     "low": 318.0, "close": 324.0, "volume": 210400}
  ]
}
```

`symbol` in the path is uppercased. With `market=ph` the lookup and the
`symbol` field use the `.PS` key. SQL is `load_daily_ohlcv_rows`:

```sql
SELECT ts, bar_date, open, high, low, close, volume
FROM daily_bars
WHERE symbol = %s
  AND ts > %s          -- only when after_ts is set
ORDER BY ts;           -- or ORDER BY ts DESC LIMIT n, then re-sorted ascending
```

Path segments are percent-encoded, so a ticker like `FLG/PU` stays one
segment.

### `GET /api/history/{symbol}/meta`

Query: `market`. 404 `{"detail": "Unknown symbol."}` when `symbols` has no
row.

```json
{"symbol": "AAPL", "market": "us", "last_bar_ts": 1794499200,
 "row_count": 4552, "last_hourly_bar_ts": null}
```

### `POST /api/history/bulk`

Body (`HistoryBulkRequest`): `symbols` (max 500), `after_ts`, `limit`
(1–2000), `market` (`us` or `ph`).

```json
{"results": {"AAPL": [{"ts": 1794499200, "date": "2026-11-13", "open": 1.0,
  "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}], "ZZZZ": []}}
```

Keys are the caller's tickers (`BDO` stays `BDO` when `market=ph`; the
query uses `BDO.PS`). A symbol with no rows is present with `[]`.
`data/history_bulk.py` runs one `unnest` + `LATERAL` query against
`(symbol, ts)` when `limit` is set, and `WHERE symbol = ANY(%s)` otherwise.

## Python client

`data/history_client.py`. Base URL is `STOCKS_HISTORY_URL`, or
`https://33ai.rootlabs.dev` when that is empty. Basic auth comes from
`settings.stocks_history_auth`: `STOCKS_HISTORY_USERNAME` or
`WEB_UI_USERNAME`, and `STOCKS_HISTORY_PASSWORD` or `WEB_UI_USERNAME`.

One shared `httpx.Client` (gzip, keepalive). At most 4 requests in flight
(`inflight_slots` raises that for a paper-stream preload). Connect timeout
20 s, read timeout 30 s. Transport errors retry 3 times with
`0.4s × attempt` backoff. The client is not closed on a timeout, so one
handshake failure does not drop the other in-flight calls.

| Function | HTTP | Empty vs failure |
|---|---|---|
| `fetch_history_symbols` | `GET /api/history/symbols` | `None` on error |
| `fetch_history_bars` | `GET /api/history/{symbol}` | `[]` on 404, `None` on transport / 5xx |
| `fetch_history_meta` | `GET /api/history/{symbol}/meta` | `None` on 404 or error |
| `fetch_history_bars_bulk` (`data/history_bulk.py`) | `POST /api/history/bulk`, 200 symbols per request | `None` when the route is missing (404/405) or the first chunk fails; later chunk failures keep the symbols already fetched |

`data/history.py` turns bars into candles and DataFrames:

| Function | Returns |
|---|---|
| `list_history_symbols(market=)` | symbol metas; `[]` if the API returns nothing |
| `load_daily_bars(symbol, after_ts=, limit=, market=)` | bar dicts, `[]` if the symbol has none, `None` on hard failure |
| `load_daily_candles` / `load_daily_ohlcv_df` | candles or an OHLCV frame indexed by bar time |
| `load_daily_tape_rows` | `{open, high, low, close, volume, timestamp}` for the paper stream. `None` means retry; `[]` means no bars |
| `fetch_ohlcv_candles(symbol, timeframe)` | daily bars, or weekly bars resampled `W-FRI` from daily |
| `load_hourly_bars` / `load_hourly_candles` | hourly rows, **only** on the owner. Returns `None` on a laptop because there is no hourly HTTP route |

`resample_chart_frame` is the chart adapter: it reindexes daily bars onto
the market session calendar and aggregates a weekly chart. Scanner candle
timestamps are left alone.

## Where the bars are used

- **Charts and explorer.** `web/services.py` and `data/tv_client.py` call
  `fetch_ohlcv_candles` / `load_daily_candles` while `--web` is up.
  Replay charts call `load_daily_ohlcv_df` then `resample_chart_frame`
  (`POST /api/replay/chart`).
- **Paper stream.** `data/stream_server.py` preloads with
  `load_daily_tape_rows_bulk` and polls one symbol with
  `load_daily_tape_rows`. On a laptop that is `POST /api/history/bulk`
  (falling back to per-symbol `GET` if bulk is missing). On the owner it
  is the bulk SQL. The scanner keeps those candles in `OHLCVStore`; it
  does not query Postgres itself.
- **Backtests.** `core/backtest_service.py` loads daily candles through
  `load_daily_candles` and caches them. `core/backtester.py` and
  `core/rule_backtest.py` call `fetch_ohlcv_candles`. The PH symbol list
  for a backtest comes from `list_history_symbols(market="ph")`.
- **MCP.** `mcp_server/tools_market.py` proxies the same routes:
  `get_history_symbols`, `get_history_bars`, `get_history_meta`. History
  calls authenticate with `WEB_UI_USERNAME` as both user and password
  (`mcp_server/config.py::history_credentials`). `get_history_bars`
  clamps `limit` to `MCP_MAX_HISTORY_BARS` (default 2000) and sends that
  cap when `limit` is omitted.
- **Datasets.** `learn/dataset.py` lists symbols and loads frames through
  the facade. Kronos training on the owner reads `daily_bars` from
  Postgres directly; that path is not this HTTP API.

## Settings

| Setting | Role |
|---|---|
| `DATABASE_URL` | Owner's socket DSN for `stocks_history`. Readers that use the HTTP API do not need it. |
| `STOCKS_HISTORY_OWNER` | `true` on the VPS so it writes Postgres and serves `/api/history` instead of calling itself. |
| `STOCKS_HISTORY_URL` | Remote API base. Empty on a laptop `--web` becomes `https://33ai.rootlabs.dev`. Leave empty on the owner. |
| `STOCKS_HISTORY_USERNAME` / `STOCKS_HISTORY_PASSWORD` | Basic auth for the remote API. Empty username falls back to `WEB_UI_USERNAME`; empty password falls back to `WEB_UI_USERNAME` as well. |
| `WEB_UI_USERNAME` | The value the server expects on both sides of history Basic auth. |
