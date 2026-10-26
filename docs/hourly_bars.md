# Plan: Populate Hourly OHLCV into `stocks_history`

## Goal

Populate **4 years** of 1-hour OHLCV bars for every symbol in the `symbols` table
(~18 000 symbols) into the `stocks_history` PostgreSQL database, alongside the
existing `daily_bars` table.

---

## Current State

| Item | Detail |
|------|--------|
| DB | `stocks_history`, local PostgreSQL, peer auth (`r00t`) |
| `symbols` table | 18 001 rows (17 719 US, 282 PH) |
| `daily_bars` table | 47.6 M rows, date range 1972-06-01 → 2026-10-23 |
| Daily fetch | `data/update.py` — Yahoo v8 chart API (`interval=1d`, `range=2y`) |
| Fill-all daily | `data.tv_client.fetch_yahoo_daily_max` — `interval=1d`, `range=max` |
| Existing timeframe map | `data/tv_client.MCP_TIMEFRAME_MAP` already maps `"1h": "1h"` |
| Pattern default | `patterns/base_pattern.py` patterns default to `["1h"]` — they *expect* hourly data |

---

## Constraint Analysis

### Yahoo v8 chart API does NOT support historical hourly data

**Verified by live test.** Yahoo Finance's `v8/finance/chart` endpoint does not return
historical intraday bars. Testing confirmed: `interval=1h&range=max` returns only ~1
trading day of bars (6-8 candles) with the rest of the requested history silently
dropped. This affects all tested symbols (AAPL, SPY, MSFT, TSLA).

| Interval | Historical depth |
|----------|-----------------|
| `1d`     | 30+ years (full history) |
| `1wk`    | 30+ years |
| `1h`     | ~1 trading day only |
| `30m`    | ~1-2 trading days |

**Conclusion: Yahoo is NOT a viable data source for hourly bars.**

### Required: external data source for hourly OHLCV

To get 4 years of hourly data you need a provider that stores historical intraday bars.

| Provider | Hourly history | Cost | Notes |
|----------|---------------|------|-------|
| **Polygon.io** | 5+ years minute-level | Paid (~$199/mo) | Best quality; bulk download |
| **Twelve Data** | 5+ years hourly | Free tier / Paid | Hourly endpoint available |
| **Alpha Vantage** | Limited intraday history | Free tier / Paid | Intraday history is shallow |
| **IEX Cloud** | Historical minute data | Paid | Good US equities |
| **Yahoo** | Historical hourly does not exist | Free | Not viable |
| **TradingView screener** | Screener only, no bulk history | Free | Live/most-recent, not historical |
| **Local scrape** | N/A | Free + fragile | Scrape TV charts; rate-limit risk |

**Recommendation: Polygon.io** — most reliable historical hourly (and minute) data,
supports bulk download, well-documented API. Cost is ~$199/mo for full access.

### Scale

| Metric | Value |
|--------|-------|
| Symbols | 18 001 |
| Bars per symbol (4 y) | ~6 500 (US) / varies (PH) |
| Total bars | ~117 million |
| Storage estimate | ~8-12 GB |

---

## Proposed Approach

### 1. Database Schema

Create an `hourly_bars` table mirroring `daily_bars`:

```sql
CREATE TABLE IF NOT EXISTS hourly_bars (
    symbol   TEXT NOT NULL,
    ts       BIGINT NOT NULL,
    bar_date DATE NOT NULL,
    open     DOUBLE PRECISION NOT NULL,
    high     DOUBLE PRECISION NOT NULL,
    low      DOUBLE PRECISION NOT NULL,
    close    DOUBLE PRECISION NOT NULL,
    volume   BIGINT NOT NULL,
    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS hourly_bars_bar_date_idx ON hourly_bars (bar_date);
```

- `ts` = Unix seconds of bar open time (UTC)
- `bar_date` = `ts` cast to `America/New_York` date (US) or `Asia/Manila` date (PH)
- Same insert pattern as `daily_bars`, but hourly preserves existing rows:
  `ON CONFLICT (symbol, ts) DO NOTHING`
- `symbols` table gets an optional `last_hourly_bar_ts` column for tracking

### 2. Fetch Strategy — Alpaca Markets (current)

Use Alpaca's [Historical Bars API](https://docs.alpaca.markets/reference/stockbars):

```
GET https://data.alpaca.markets/v2/stocks/bars?symbols=A,B,C&timeframe=1Hour
    &start=...&end=...&limit=10000&feed=sip&adjustment=raw
```

Verified behaviour on the Basic (free) plan:

- 200 requests/min, SIP history since 2016, latest 15 minutes restricted.
- **1-hour pages hold only ~200 data points (~12 sessions)** regardless of
  `limit`, so a multi-year backfill is page-bound; a one-session incremental
  run is batch-bound and fast.
- Multiple symbols per request are supported. Class shares use dots (`AGM.A`),
  not hyphens; warrants/units (`AAC-UN`, `ACHR-WT`) are not served.
- OTC symbols need the paid OTC feed (HTTP 403 on Basic), so they are skipped.
- Junk/unknown tickers can 400 an entire batch; the updater splits the batch and
  retries smaller groups.
- Extended-hours bars (04:00–19:00 ET) are included, matching existing rows.
- **Existing rows win:** per-symbol coverage is read from `hourly_bars` before
  fetching, only the missing tail is requested, and inserts use
  `ON CONFLICT (symbol, ts) DO NOTHING`.

#### Fetch workflow per symbol:

1. Read `min(ts)` / `max(ts)` for the symbol from `hourly_bars`.
2. Skip if the newest bar is at/after the last closed session.
3. Otherwise fetch from `max(ts) - 1 day` (fills any boundary gap).
4. Symbols with no rows backfill from the 4-year default window.
5. Batch symbols, page through `next_page_token`, COPY to staging, merge with
   `DO NOTHING`, then advance `symbols.last_hourly_bar_ts`.

### 3. New Module: `data/update_hourly.py`

Mirror `data/update.py`'s structure:

```
data/update_hourly.py
├── _fetch_symbol(conn, symbol, polygon_client)  →  # single-symbol fetch
├── _fetch_batch(symbols, polygon_client, fetch_limit)  →  # parallel batch
├── run_update(polygon_api_key, fetch=True, fetch_limit=None)  →  # main entry
└── _last_trading_date()  →  # market-aware date logic (shared with update.py)
```

Key design decisions:

- **Workers**: 3 concurrent workers (matching `update.py`)
- **Rate limiting**: Polygon free tier = 5 req/min; Starter = 5 req/min; Pro = 30 req/min
  Use a semaphore/token-bucket limiter. Configurable via env var `POLYGON_RATE_LIMIT`.
- **Bulk loading**: Use `copy_bars` + `flush_stage` from `data/db.py`
- **Progress tracking**: Write `logs/hourly_bars_updated.txt` (daily stamp pattern)
- **CLI**: `python main.py --update-hourly` or `python -m data.update_hourly`
- **Resume**: Track per-symbol `last_hourly_bar_ts`; only fetch bars newer than existing
- **Idempotent**: `ON CONFLICT (symbol, ts) DO UPDATE` makes reruns safe
- **Error handling**: Skip failed symbols, log to `logs/hourly_bars_errors.log`

### 4. CLI Integration

Add to `main.py`'s argument parser:

```
--update-hourly            Populate hourly bars (runs data.update_hourly)
--update-hourly-fetch-limit N   Cap symbols to fetch
--update-hourly-no-fetch     Just init schema + stamps, skip network
--polygon-api-key KEY      Polygon.io API key (or set POLYGON_API_KEY env)
```

### 5. API Integration

The existing `mcp__trading_bot__get_history_bars` tool only serves daily bars from
`daily_bars`. After adding hourly:

- Add `mcp__trading_bot__get_history_bars_hourly` tool (or make existing tool accept
  a `granularity` parameter: `"1d"` / `"1h"`)
- Add `hourly_bars` to the API routes in `web/app.py` (`api_history_bars`)
- Add MCP tool schema for hourly bars in `web/app.py`

### 6. Estimated Time (Alpaca Basic)

| Metric | Value |
|--------|-------|
| API limit | 200 req/min (configured 170 for margin) |
| Page size (1Hour) | ~200 bars (~12 sessions) |
| Full 4-year backfill | page-bound: ~0.9 s/request, rate limit binds at 8 workers |
| Incremental run | one session per symbol; 25-symbol batches make this minutes |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Yahoo has no historical hourly data | Plan needs external source | Switch to Polygon.io or alternative |
| Alpaca 1-hour pages ~200 bars | Slow multi-year backfill | 8 workers hit the 200/min limit; incremental runs stay cheap |
| PH symbols have no hourly on Alpaca | Missing PH data | Accept daily for PH |
| Disk / memory pressure loading 117 M rows | OOM / slow | Use `copy_bars` (COPY protocol); process per-market chunks |
| Long-running job interrupted | Partial state | Idempotent inserts + per-symbol resume from `hourly_bars` |
| Alpaca Basic omits OTC/warrants | Partial universe | Skipped and logged; paid feed needed for OTC |

---

## Completed

All core files have been implemented and verified:

| File | Status |
|------|--------|
| `data/db.py` | ✅ `hourly_bars` schema, COPY staging, `DO NOTHING` merge, `hourly_coverage()`, `set_last_hourly_bar_ts()` |
| `data/alpaca_client.py` | ✅ New — batched + paginated bars client, shared rate limiter, asset map, 400 isolation |
| `data/update_hourly.py` | ✅ New — gap-fill updates, OTC/unsupported filtering, batch workers, progress + stamp |
| `main.py` | ✅ `--update-hourly` and `--update-hourly-limit` use Alpaca settings |
| `config.py` | ✅ `alpaca_*`, `hourly_fetch_batch_size`, `hourly_fetch_workers` |

### Usage

```bash
# Full update: every stale symbol Alpaca can serve, 170 req/min
.venv/bin/python main.py --update-hourly

# Dry run: cap to the first N stale symbols
.venv/bin/python main.py --update-hourly --update-hourly-limit 10

# Credentials live in .env:
# ALPACA_API_KEY=...
# ALPACA_SECRET_KEY=...
# ALPACA_DATA_FEED=sip
```

### What to expect

- **Existing rows are preserved.** Coverage comes from `hourly_bars`, only the
  missing tail is fetched, and conflicts are skipped.
- **Incremental runs** fetch from each symbol's last stored bar (one-day overlap).
- **OTC (~6.6k) and non-Alpaca tickers (~2.2k)** are skipped and logged; the
  Basic plan has no OTC feed.
- **Stamp tracking**: `logs/hourly_bars_updated.txt` (advanced only on a full,
  error-free pass).
- **PH symbols skipped**: Alpaca covers US equities only.

### Rate limiting

Alpaca Basic = 200 requests/minute. The client paces every request through a
process-wide limiter (default 170/min), retries 429/5xx with exponential
backoff, and splits a batch on HTTP 400 to isolate bad tickers. Eight batch
workers keep the limiter saturated.

### After data is loaded

- Patterns already default to `[\"1h\"]` and will read from `hourly_bars` via the new
  `load_hourly_ohlcv_rows()` function
- API endpoint integration (`web/app.py`) can be added next to expose hourly bars
  alongside daily in `api_history_bars`
- Consider a cron job for daily incremental updates during market hours

---

## Implementation Order (COMPLETED)

1. **Schema migration** — add `hourly_bars` table and `last_hourly_bar_ts` column
2. **Single-symbol fetch test** — verify Alpaca bars work, parse, insert
3. **Batch fetch script** — `data/update_hourly.py` with parallel workers + rate limiter
4. **CLI integration** — `--update-hourly` in `main.py`
5. **Stamp file + progress** — `logs/hourly_bars_updated.txt`
6. **API/MCP hook** — expose hourly bars via same mechanism as daily
7. **Cron job** — hourly incremental updates (market-hours only)

---

## Key Files to Touch

| File | Change |
|------|--------|
| `data/db.py` | `hourly_bars` schema, `copy_hourly_bars()`, `flush_hourly_stage()` (DO NOTHING), `hourly_coverage()` |
| `data/update_hourly.py` | **New** — gap-fill hourly fetch (mirrors `update.py`) |
| `data/alpaca_client.py` | **New** — Alpaca bars client (batches, pagination, assets, rate limit) |
| `main.py` | Add `--update-hourly` argument |
| `web/app.py` | Add hourly bars to `api_history_bars` or new route |
| `config.py` | Add `alpaca_*`, `hourly_fetch_batch_size`, `hourly_fetch_workers` |
| `scripts/` | Optional: shell script for cron-style hourly updates |
| `docs/hourly_bars.md` | This document |
