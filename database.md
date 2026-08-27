# Plan: PostgreSQL stock-history database

## Current

History lives in Postgres `stocks_history` and is served as `GET /api/history`.
`--update-db` refreshes stale symbols from Yahoo/PSE. CSV files are not a
history source (ingest from `/home/r00t/stocks_data` was removed).

The notes below are the original design of the schema and daily update.

---

## Objective (original)

Move historical daily OHLCV into PostgreSQL and keep it current from Yahoo/PSE
so the bot can query history without files on disk.

1. **Store** daily bars in PostgreSQL.
2. **Verify** the database is current (`--check-db` freshness).
3. **Update** every trading day via `--update-db` (Yahoo v8 / PSE Edge).

---

## 1. Current state (verified)

### Data on disk — `/home/r00t/stocks_data`

- Layout: `<FIRST_LETTER>/<TICKER>.csv`, e.g. `A/AAAC.csv`, `B/BABA.csv`,
  `M/MSFT.csv`. One CSV per symbol.
- Scale: **17,719 CSVs**, ~3.7 GB, across letter dirs `A`–`Z` (27 entries incl.
  `.`).
- `1.txt` is just a `tree` listing of the directory (17,749 lines) — not data.
  Ignore it.
- Columns are always `open, high, low, close, volume, timestamp` but **the
  column order varies per file** (observed: `open,high,volume,low,close,timestamp`,
  `close,open,low,high,volume,timestamp`, `close,high,volume,low,open,timestamp`,
  etc.). Must parse by header name, never by position.
- `timestamp` = Unix seconds, one row per day. Prices are floats, `volume` is an
  integer share count written as float.
- Gaps exist: some tickers have rows with a timestamp but blank OHLCV (halted /
  delisted days) — these must be skipped, not fatal (already handled this way in
  `data/stream_server.py:88-105`).
- Freshness: latest bar in the sample files is `2026-07-10` (ts `1783690200`).
  Today is `2026-08-17` → data is ~5 weeks stale. "Check history" must surface
  this.

### Environment — PostgreSQL

- PostgreSQL **16** is running locally on port **5432** (cluster `16/main`,
  `online`).
- `psql` is on PATH.
- Auth is **peer** for local sockets: OS user `r00t` maps to role `r00t`.
  `psql -d postgres` works as-is (no password).
- Role `r00t`: `CREATEDB=yes`, `SUPERUSER=no`. It **cannot** `CREATE EXTENSION`
  or create roles, but it can create and own a database. That is enough.
- The `postgres` DB already exists and there are many unrelated project DBs —
  we must create a dedicated, clearly-named DB and touch nothing else.

### Codebase

- No PostgreSQL/psycopg/SQLAlchemy dependency exists yet. `requirements.txt`
  has no DB driver.
- Existing CSV parsing references (reuse the conventions):
  - `data/stream_server.py:_load_symbol_csv` — `csv.DictReader`, float coercion,
    skip blank rows, sort by timestamp.
  - `learn/dataset.py:iter_ticker_frames` — glob `*/*.csv`, pandas, dedupe on
    timestamp.
- Fresh daily history source already exists for updates:
  - US: `data/tv_client.py:_fetch_history_chart` (Yahoo v8 chart).
  - PH: `data/pse_edge.py:fetch_daily` / `fetch_history`.
- `config.py` uses `pydantic-settings` (reads `.env`) — add DB settings there.
- `tqdm` and `schedule` are already dependencies.

---

## 2. Design

### Database + schema

Create DB **`stocks_history`**, owned by `r00t` (peer auth, no password needed
for a socket connection).

Table **`daily_bars`** (the canonical data store):

| column     | type              | notes                                             |
|------------|-------------------|---------------------------------------------------|
| symbol     | TEXT              | ticker, uppercase                                 |
| ts         | BIGINT            | Unix seconds (matches CSV exactly)                |
| bar_date   | DATE              | derived from `ts` in `America/New_York` (market day) |
| open       | DOUBLE PRECISION  | (or NUMERIC(18,6) — see notes)                    |
| high       | DOUBLE PRECISION  |                                                   |
| low        | DOUBLE PRECISION  |                                                   |
| close      | DOUBLE PRECISION  |                                                   |
| volume     | BIGINT            | share count (cast from float)                     |

- **PRIMARY KEY `(symbol, ts)`** → makes ingest idempotent via `ON CONFLICT`.
- **INDEX on `bar_date`** for date-range and freshness queries.
- Store `ts` (not just `date`) as the key because that is the file's identity
  and is unambiguous across markets; `bar_date` is a convenience column.

Table **`symbols`** (metadata, powers the "check history" report):

| column        | type              | notes                          |
|---------------|-------------------|--------------------------------|
| symbol        | TEXT PRIMARY KEY  |                                |
| letter        | TEXT              | `A`–`Z` directory             |
| market        | TEXT              | default `us`                  |
| source_path   | TEXT              | CSV path on disk              |
| last_bar_ts   | BIGINT            | max ts in `daily_bars`        |
| row_count     | BIGINT            | bars loaded for this symbol   |
| updated_at    | TIMESTAMPTZ       | last ingest time              |

### Driver

Use **psycopg3** (`psycopg[binary]`) — modern, stdlib-friendly, has `copy`
for bulk load. Add to `requirements.txt`.

### Data layer — new `data/db.py`

Small module encapsulating connection + upsert + freshness queries, so scripts
and (optionally) the scanner share one code path:

- `get_conn()` — build from `settings.database_url`.
- `ensure_schema()` — `CREATE DATABASE` is a one-time manual step, but this
  creates tables/indexes idempotently.
- `upsert_bars(rows)` — `INSERT ... ON CONFLICT (symbol, ts) DO UPDATE`.
- `copy_bars(symbol, rows)` — `cursor.copy` for the bulk ingest path.
- `max_ts(symbol)` / `all_max_ts()` — freshness lookups.
- `summary()` — per-symbol / global row counts + last-bar dates.

---

## 3. Implementation steps

### Step 0 — Config

1. `config.py`: add DB settings to `Settings`:
   - `database_url: str = "postgresql://r00t@/stocks_history?host=/var/run/postgresql"`
   - optional discrete fallbacks `db_host/db_port/db_name/db_user/db_password`.
2. `.env.example` (and `.env`): add a `# ── PostgreSQL ──` block documenting
   `DATABASE_URL`.
3. `requirements.txt`: add `psycopg[binary]>=3.1`.

### Step 1 — Ingest history

New command `python main.py --ingest-db` (plus `--ingest-db-dir` override,
default `/home/r00t/stocks_data`), implemented in `data/ingest.py`:

1. Ensure schema exists (`ensure_schema`).
2. Enumerate `data_dir/*/*.csv` (skip `1.txt`, any non-CSV).
3. Per file:
   - Parse with `csv.DictReader` (header-name access — handles reordered columns).
   - Coerce OHLC to float, volume to int, ts to int; **skip** blank/gap rows.
   - Sort by `ts`, drop duplicate `ts` (keep last), derive `bar_date` in
     `America/New_York`.
   - Bulk-insert with `cursor.copy` into a temp table → `INSERT ... ON CONFLICT
     (symbol, ts) DO UPDATE` (or direct COPY into `daily_bars` staging).
4. Upsert the `symbols` metadata row (letter, path, last_bar_ts, row_count,
   updated_at).
5. Progress via `tqdm`; log a final summary (files ingested, rows, elapsed).
6. Resumable/idempotent: safe to re-run — conflicts are no-ops.

Estimated scale: ~17.7k files / ~5M rows. COPY is required for a reasonable
runtime (target: a few minutes, not tens).

### Step 2 — Verify / check history

New command `python main.py --check-db` (`data/check.py` or a method in
`data/db.py`), producing a report:

- Global: total symbols, total bars, min/max `bar_date`, DB size.
- Per symbol (or top-N stale): `min/max bar_date`, row count, CSV vs DB row
  mismatch, CSV vs DB last-bar mismatch.
- **Freshness**: `days_since_last_bar` per symbol and aggregate histogram;
  flag any symbol whose DB last bar < CSV last bar, and any whose CSV last bar
  is older than N days (surfaces the ~5-week staleness).
- Exit code non-zero when any symbol is stale/missing, so it can gate cron.

### Step 3 — Daily update

New command `python main.py --update-db` (idempotent, run after market close),
which saves each day's closing bars in the same `open,high,low,close,volume,
timestamp` shape:

1. **CSV sync (primary path, matches the task's "file formats"):** for each
   symbol, read `max(ts)` in `daily_bars`; read the CSV; upsert only rows with
   `ts > max_ts`. Skip files whose mtime is unchanged since last ingest (track
   in `symbols.updated_at` + file mtime/size) to keep the daily pass cheap.
2. **Fetch fallback (fills the staleness gap):** for symbols still older than
   the last trading day, fetch the latest daily bars via the existing sources —
   `TVClient._fetch_history_chart` (US) and `pse_edge.fetch_daily` (PH) — and
   upsert them. This covers both "CSV got new rows" and "CSVs are stale, pull
   fresh closes".
3. Recompute `symbols.last_bar_ts` / `row_count` / `updated_at`.
4. Log a summary (symbols updated, new bars, errors).

Scheduling options (choose one; both are documented):
- **cron**: weekday `*/15` + `python -m data.update_cron` (runs after 16:30 ET;
  do not use `CRON_TZ` — Ubuntu vixie cron ignores it).
- **in-process**: a `--db-daemon` mode using the already-present `schedule`
  lib, if the bot already runs long-lived.

---

## 4. Files to add / change

| file                    | change                                                       |
|-------------------------|--------------------------------------------------------------|
| `data/db.py`            | NEW — connection, schema, upsert/copy, freshness queries     |
| `data/ingest.py`        | NEW — bulk historical ingest + summary                       |
| `main.py`               | add `--ingest-db`, `--check-db`, `--update-db` (+ dir args)  |
| `config.py`             | add DB settings to `Settings`                                |
| `requirements.txt`      | add `psycopg[binary]>=3.1`                                   |
| `.env` / `.env.example` | add `DATABASE_URL` (documented)                              |
| `database.md`           | this plan                                                     |

---

## 5. Verification / acceptance criteria

1. `python main.py --ingest-db` completes, logs ~17.7k files and a row count
   matching `find /home/r00t/stocks_data -name '*.csv' | wc -l`.
2. `psql -d stocks_history -c "SELECT count(*) FROM daily_bars"` returns a
   plausible total (~millions); spot-check: `SELECT * FROM daily_bars WHERE
   symbol='BABA' ORDER BY ts DESC LIMIT 3` matches the CSV.
3. Re-running `--ingest-db` is a no-op (idempotent) — row counts unchanged.
4. `python main.py --check-db` reports last bar `2026-07-10` and flags the
   ~5-week staleness with a non-zero exit code.
5. `python main.py --update-db` fills the gap (CSV sync + fetch fallback), and
   a second `--check-db` shows last bar ≈ latest trading day and exits 0.
6. No unrelated databases touched (create only `stocks_history`).

---

## 6. Risks / decisions

- **Column order varies** — solved by `DictReader` name access; never index the
  columns.
- **Blank gap rows** — skip row (log at debug), don't fail the symbol.
- **Volume float→int** — cast with `int(float(v))`; any fractional volumes would
  be rounded (none observed in US share data). If exactness matters later,
  switch `volume` to `NUMERIC(20,4)`.
- **Prices as DOUBLE vs NUMERIC** — DOUBLE matches the CSVs exactly and is what
  `pandas`/patterns already use; NUMERIC is a stricter alternative if ledger
  accuracy is required. Start with DOUBLE.
- **`r00t` is not superuser** — fine: `CREATE DATABASE stocks_history` once as
  `r00t`, then connect via socket (peer auth, no password). Do not attempt
  `CREATE EXTENSION` (not needed here).
- **3.7 GB daily re-read cost** — mitigated by mtime tracking + only upserting
  rows `ts > max_ts`, and by COPY for the bulk path.
- **Timestamps vs calendar date** — key on `ts` (file identity); derive
  `bar_date` in `America/New_York` for reporting only.

## 7. Suggested rollout order

1. DB config + `data/db.py` + `ensure_schema` (Step 0).
2. `--ingest-db` bulk load + idempotency (Step 1).
3. `--check-db` report (Step 2).
4. `--update-db` incremental + fetch fallback + cron (Step 3).
5. Verify against acceptance criteria (Section 5).

## 8. Remote history API (local `--ui` / `--web` / Kronos)

The VPS (`https://33ai.edos.uk`) is the only machine that should run Postgres
and `--ingest-db` / `--update-db`. It exposes authenticated read-only endpoints:

- `GET /api/history/symbols`
- `GET /api/history/{symbol}` (optional `?after_ts=`)
- `GET /api/history/{symbol}/meta`

Auth: session cookie or HTTP Basic with `WEB_UI_USERNAME` as both user and
password (default `admin` / `admin`). Dashboard login still uses
`WEB_UI_PASSWORD`.

Laptops running `--ui` / `--web` always use `https://33ai.edos.uk` (auto-set
if `STOCKS_HISTORY_URL` is empty). Charts and daily OHLCV do not fall back
to Yahoo/TV. History client Basic auth defaults to
`WEB_UI_USERNAME`:`WEB_UI_USERNAME` unless
`STOCKS_HISTORY_USERNAME` / `STOCKS_HISTORY_PASSWORD` are set. On the VPS
set `STOCKS_HISTORY_OWNER=true` and leave `STOCKS_HISTORY_URL` empty so
`--web` does not HTTP-loop to itself. Do not dump
the whole universe in one response — clients fetch per symbol.
