# PSE history plan (stocks_history → 33ai.edos.uk)

**Status:** live on 33ai.edos.uk — PH rows imported (`BDO.PS` / `market=ph`); `--update-db` / `--check-db` cover both books. Local laptop `--web` reads PSE via the history API.
**Date:** 2026-08-27
**Goal:** load Philippine Stock Exchange daily OHLCV into local Postgres `stocks_history`, then dump/import those rows onto the Contabo VPS that serves `https://33ai.edos.uk`, so `--market ph` paper/backtest can read PSE bars the same way the US book reads Yahoo-backed bars.

This is the **history/data** slice. Trading rules (PHP ledger, lots, long-only, session clock) already live in `core/market.py` and `philippine_stocks.md`. This file does not re-plan the broker.

---

## How we move forward (locked)

**Lock `.PS` keys, ingest locally, COPY only PH rows onto 33ai.** Do not add `market` to the PK and do not dump the 5.86 GB US database.

That is the cheapest path that cannot smash US `SM` / `TEL` / `GLO` / `AP`. `market='ph'` is a **label**; uniqueness still lives on `symbol`.

**Order:**

1. **Spike Edge** — paginate the directory; pull `BDO` and `SM` in year chunks; confirm ≥450 bars and Manila calendar dates. If Edge is broken, stop.
2. **Write PH as `BDO.PS`** — insert `symbols` (`market='ph'`), Manila `bar_date`, COPY into local `stocks_history` (create that DB; PH-only).
3. **Teach reads** — `GET /api/history/BDO?market=ph` → `BDO.PS`. Bot ticker stays `BDO`.
4. **Import on VPS** — PH-only COPY + `ON CONFLICT`. Guard: incoming symbols must be `%.PS` / `market=ph`. Recheck US count still 17,719.
5. **Same `--update-db` / `--check-db` on 33ai** — after PH rows exist, those commands on the VPS must keep **both** books current. PH via Edge, US via Yahoo. Do not add a second ingest tool for daily refresh. Add a Manila after-close cron that runs the **same** `--update-db`; keep the US after-close cron. Each pass is per-market: only stale `ph` rows at 15:30 PHT, only stale `us` rows after 16:30 ET (the other book is a no-op).

Do not wait on a `(market, symbol)` migration. Revisit that later only if the `.PS` suffix in the table becomes a problem.

---

## 0. What we verified (2026-08-27)

### 33ai.edos.uk (Contabo `contabo-edos`, host `vmi3391975`)

- App: `/home/deploy/apps/trading`
- Web: `trading-web.service` → `python main.py --web` on `127.0.0.1:8080` (uvicorn). Public HTTPS is `https://33ai.edos.uk` (HTTP/2, Host header reaches uvicorn).
- Postgres: Docker `trading-postgres` (`pgvector/pgvector:pg16`), bound **only** to `127.0.0.1:5437`. App DSN is `postgresql://trading@127.0.0.1:5437/stocks_history` (password in VPS `.env`, not repeated here).
- Cron: weekday `30 16 * * 1-5` `python main.py --update-db` under `CRON_TZ=America/New_York`. Ubuntu vixie cron **ignores** `CRON_TZ`; the intended design in `data/update_cron.py` is `*/15` + NY 16:30 gate. Live crontab is **US-close only**. After PH import it must still call `python main.py --update-db`, plus a second gate after PSE close (15:30 Asia/Manila) that calls the **same** command.

### `stocks_history` on the VPS (live)

| | |
|---|---|
| Size | ~5.86 GB |
| Tables | `daily_bars` (heap ~4.1 GB), `symbols` (~2.5 MB) |
| Symbols | **17,719**, all `market = 'us'` |
| Bars | **46,130,413** |
| Date span | `1972-06-01` … `2026-08-26` |
| PK | `daily_bars (symbol, ts)`; `symbols (symbol)` |
| `bar_date` | derived as `(to_timestamp(ts) AT TIME ZONE 'America/New_York')::date` |

**Zero PH rows.** `symbols.market` has no `ph`.

Ticker collisions with real PSEi names already stored as **US** tapes:

| Symbol | VPS `market` | Meaning if we stored PSE as the same key |
|---|---|---|
| `SM` | us | Would overwrite / mix SM Investments with US SM |
| `TEL` | us | Same |
| `GLO` | us | Same |
| `AP` | us | Same |

`BDO`, `ALI`, `JFC`, `SMPH`, … are free today, but one collision is enough: **do not store PSE as a bare ticker** in this schema.

### This laptop

- Postgres 16 on `5432`, peer auth as `r00t`.
- **No** database named `stocks_history` yet. `.env` already points at `postgresql://r00t@/stocks_history?host=/var/run/postgresql`.
- Local `--ui` / `--web` **do not read local Postgres**. Readers hit `GET /api/history` on 33ai (`data/history.py`, `data/history_client.py`). Local Postgres is only for `--update-db` / `--check-db` and for building the dump.

### Code that already exists (reuse, do not reinvent)

- `data/pse_edge.py` — PSE Edge `DisclosureCht.ax` daily OHLC. Yahoo `*.PS` is a **YHD stub** (no timestamps). Volume is **peso VALUE / CLOSE**, not official share volume.
- Default Edge window: last **800 calendar days** (~2y). Throttle **0.4s** between HTTP posts. Directory lookup is **one keyword search**, not a full listed-universe crawl. Disk cache: `data/cache/pse_edge_directory.json` (7d) and `data/cache/pse_edge_ohlcv/{SYM}_1d.json` (6h).
- `data/update.py` already branches `market == "ph"` → `fetch_daily`, but **`--update-db` is not PH-safe today:**
  - It only walks rows already in `symbols` (no PH seed — ingest/import still required).
  - Stale detection uses a **single** US `_last_trading_date` (16:00 America/New_York) for every ticker.
  - `upsert_bars` always derives `bar_date` in New York.
  - `fetch_daily(symbol)` is fine if `symbol` is `BDO.PS` (`_norm_symbol` strips `.PS`).
- `data/check.py` / `--check-db` is one global report: `today` and age vs **New York**, one `max(bar_date)`, no `market` split. A current US tape would hide a stale PH book (and the reverse after Manila `bar_date`).
- `data/update_cron.py` and `ensure_history` stamp / median are US-session only.
- `db.refresh_symbol_meta` is `UPDATE symbols … WHERE symbol = %s` — it will not insert a new ticker.
- Scanner/TV path for PH already uses Edge (`TVClient` + `philippines` / `PSE`). Kronos lookback wants **≥400 daily bars**.

---

## 1. Design decisions (lock these before coding)

### 1.1 Identity: store PSE as `TICKER.PS`

**Decision:** `daily_bars.symbol` / `symbols.symbol` for PH is Yahoo-style **`BDO.PS`**, `symbols.market = 'ph'`, `letter` = first letter of the **bare** ticker (`B`).

Reasons:

- No PK change on 46M US rows.
- Dump/import is insert-only; cannot clobber US `SM`.
- Matches `core/market.py` `yahoo_suffix = ".PS"` and existing `_norm_symbol` in `pse_edge.py`.

Bot / screener keep using **`BDO`**. History read path must resolve `BDO` → `BDO.PS` when market is `ph` (API query `?market=ph` and/or suffix fallback). Never return US `SM` bars for a PH scan.

**Rejected:** composite PK `(market, symbol, ts)` as the first step — correct but a 46M-row migration on the VPS before we have any PH data. Revisit later if we want first-class multi-market keys.

**Rejected:** separate database `pse_history` — doubles ops; 33ai already has one history API.

### 1.2 `bar_date` timezone

US rows stay `America/New_York`.

PH `ts` from Edge is **Asia/Manila** midnight (see `_parse_chart_date`). Deriving `bar_date` in New York shifts many sessions **one calendar day earlier** (Manila 00:00 = previous afternoon ET).

**Decision:** PH upserts compute `bar_date` as `(to_timestamp(ts) AT TIME ZONE 'Asia/Manila')::date`. Do not reuse the NY SQL in `_UPSERT_SQL` for PH. `--update-db` and `--check-db` (including on 33ai) must use last **Manila** cash session (15:00 PHT, PH holidays) for `market=ph`, and last **NY** cash session (16:00 ET) for `market=us`.

### 1.3 Universe

Phase A (ship): **all common shares we can resolve on Edge** (~280 listed names), not only PSEi 30. Scan/trade filter (`PH_MIN_ADV_PHP`, PSEi) stays in the scanner; the DB should not be the liquidity filter.

Skip or tag later: warrants, preferred (`*B`, `*W`, etc.) if Edge lists them as separate `security_id`s. First pass: store whatever the directory ticker cell matches (`[A-Z0-9][A-Z0-9.+-]*`).

### 1.4 History depth

- **Minimum:** ≥450 trading days (Kronos 400 + headroom; matches `TV_HISTORY_DAYS`).
- **Target:** Edge max we can get by **year-chunked** `startDate`/`endDate` (default 800-day single shot is not enough for a long backtest; chunk e.g. 1 Jan–31 Dec per year back to ~2010 or first chart row).
- Yahoo is **not** a source.

### 1.5 Volume

Keep `volume BIGINT` as today. Store `int(peso_value / close)`. Document that PH volume is an **approximation**. Do not mix it into US-style RVOL gates without a later ADV-in-pesos column (optional, out of scope).

### 1.6 33ai ops: `--update-db` and `--check-db` own PSE too

After the PH COPY, **the VPS does not get a new daily command.** `python main.py --update-db` and `--check-db` on `/home/deploy/apps/trading` must:

- See `symbols.market = 'ph'` / `*.PS` rows in the same `stocks_history`.
- Refresh those rows from PSE Edge when they are behind the last **closed** PSE session.
- Report PH freshness separately so a current US book cannot green-light a stale PH book.

Cron is only a timer around that same `--update-db` (Manila 15:30 **and** NY 16:30).

---

## 2. Local build (this machine)

Local DB is **PH-only** for this project. Do **not** copy the 5.86 GB US dump onto the laptop.

1. `createdb stocks_history` as `r00t` (peer). Run existing `db.ensure_schema()` so tables match VPS (`daily_bars`, `symbols`).
2. New command (name TBD, e.g. `python main.py --ingest-pse` or `python -m data.pse_ingest`):
   1. Paginate Edge company directory (`search.ax` `pageNo`) until empty; write `data/cache/pse_edge_directory.json` for **all** tickers, not one keyword.
   2. `INSERT` `symbols` rows: symbol=`BDO.PS`, market=`ph`, letter=`B`.
   3. For each ticker: chunked `fetch_daily`, map candles → `(ts, o, h, l, c, v)`, `COPY`/`upsert_bars` with **Manila** `bar_date`, then `upsert_symbol` (insert, not only `refresh_symbol_meta`).
   4. Throttle stays ≥0.4s; expect on the order of **tens of minutes to a few hours** (280 names × N year chunks), not a 17k Yahoo walk.
   5. Idempotent: PK `(symbol, ts)` so re-runs are safe.
3. Acceptance on laptop:
   - `SELECT market, count(*) FROM symbols GROUP BY 1` → only `ph`, count ≈ listed commons.
   - Spot-check `BDO.PS`, `SM.PS`: ≥450 bars, last `bar_date` = last PH session (not shifted to NY).
   - `SELECT * FROM daily_bars WHERE symbol IN ('SM','TEL')` → **empty** (those keys remain US-only on the VPS).
   - `--check-db` on this PH-only DB must report `market=ph` vs last Manila session (same code path the VPS will use).

Out of scope for this ingest: changing `trading-web` on the laptop to read local Postgres. After import, laptops keep using 33ai.

---

## 3. Dump locally, import on 33ai (must not wipe US)

**Never** `pg_restore` a full local database over VPS `stocks_history`. Local has no US rows; a full replace would delete 17k US symbols.

### Export (laptop)

Prefer **data-only SQL or custom dump of PH rows**:

- `COPY (SELECT * FROM symbols WHERE market = 'ph') TO …`
- `COPY (SELECT * FROM daily_bars WHERE symbol LIKE '%.PS') TO …`

Or `pg_dump -a -t symbols -t daily_bars` from a PH-only local DB is equivalent (entire local DB *is* PH). Compress (`gzip` / `zstd`). Size estimate: ~280 names × ~2–4k bars × ~50 bytes ≈ **tens of MB**, not gigabytes.

### Import (VPS)

SSH `contabo-edos`, copy archive to the host, load **into Docker Postgres 5437** as user `trading`:

```text
BEGIN;
-- COPY or INSERT ... ON CONFLICT DO UPDATE
-- symbols: ON CONFLICT (symbol) DO UPDATE (only rows like %.PS / market=ph)
-- daily_bars: ON CONFLICT (symbol, ts) DO UPDATE
COMMIT;
```

Guards:

- Refuse import if any incoming `symbols.symbol` has `market != 'ph'` or does not end in `.PS`.
- After load: `SELECT count(*) FROM symbols WHERE market = 'us'` still **17719**; `max(bar_date)` on non-`.PS` symbols unchanged.
- Spot `GET https://33ai.edos.uk/api/history/BDO.PS` (Basic auth as today) returns PH bars; `GET /api/history/SM` still US SM.

Restart of `trading-web.service` only if we also ship API / `--update-db` / `--check-db` code in the same deploy.

**VPS acceptance (same commands ops already use):**

```text
cd /home/deploy/apps/trading && .venv/bin/python main.py --check-db
cd /home/deploy/apps/trading && .venv/bin/python main.py --update-db
```

`--check-db` must print **US and PH** (counts, last bar, age vs that market’s last session). Exit non-zero if **either** book is stale.

`--update-db` must fetch stale `market=ph` from Edge (`BDO.PS` → Edge `BDO`) and stale `market=us` from Yahoo. After a Manila-session run, PH last bars = last PSE day; US rows unchanged. After a NY-session run, the reverse.

---

## 4. Code changes required so trading actually uses the rows

Ingest without these leaves PH bars on disk in Postgres that `--web` will not find for ticker `BDO`. `--update-db` / `--check-db` on 33ai will **not** keep PSE current until the gaps below are closed.

| Area | Change |
|---|---|
| History API | `GET /api/history/{symbol}` and `/meta`: if 404 and symbol has no `.PS`, retry `{symbol}.PS` when `?market=ph`. Prefer explicit `market` so US `SM` is unambiguous. |
| `GET /api/history/symbols` | Optional `?market=ph` so PH paper does not download 17k US metas. |
| `data/history.py` / client | When `MARKET=ph` / `get_market("ph")`, request `BDO.PS` (or `market=ph`). |
| `data/db.py` | `upsert_symbol` insert path; PH `bar_date` in Asia/Manila; `all_symbols(market=)`; per-market stats for check. |
| `--update-db` (`data/update.py`) | **One command, two books.** Stale iff `last_bar_ts` < that row’s market last **closed** session. `us` → Yahoo + NY 16:00. `ph` → `pse_edge.fetch_daily` + Manila 15:00 (PH holidays, not US). Upsert PH with Manila `bar_date`. Strip `.PS` only at the Edge client (DB key stays `BDO.PS`). Do not apply the US target_ts to `market=ph`. |
| `--check-db` (`data/check.py`) | Split report: global + **per `market`**. Age vs NY today for `us`, vs Manila today for `ph`. Histogram / stalest list tagged `us`/`ph`. Exit 1 if either book’s last bar is older than `--check-db-stale-days` vs **its** last session. Optional `--check-db --market ph` for a PH-only pass. |
| Cron on 33ai | Keep US after-close `--update-db`. Add weekday **15:30 Asia/Manila** gate that runs the **same** `python main.py --update-db` (poll + Manila clock, not `CRON_TZ` — vixie ignores it). `flock` so the US 17k walk and the PH ~280 walk cannot overlap badly; PH pass is short. |
| Stamps / web start (`history_stamp`, `ensure_history`) | Per-market last-update stamp (or median last bar **by market**). Do not let a fresh US median skip PH, or a fresh PH median skip US. |
| Seed | `--update-db` never invents PH symbols; local ingest + VPS COPY is the seed. After that, VPS `--update-db` incremental-updates `market=ph` forever. |

Scanner already knows Edge for live fetches; once 33ai has `BDO.PS` and `--update-db` keeps them fresh, `--web` / `--ui` should **stop** needing Edge on the laptop (same as US: API-only).

---

## 5. Rollout order

Same five steps as **How we move forward**. Detail under each:

1. **Spike Edge** — paginate directory; year-chunk `BDO` + `SM`; ≥450 bars; Manila `bar_date`; no Yahoo. Short spike log (tickers, bars, first/last date, failures). **Stop if Edge is broken.**
2. **Write PH as `BDO.PS`** — schema helpers (Manila `bar_date`, `upsert_symbol` insert); `createdb stocks_history`; full Edge crawl into a **PH-only** local DB; `--check-db` on that DB.
3. **Teach reads** — deploy `?market=ph` → `BDO.PS` to VPS **before** or **with** import so the UI can find rows. Bot ticker stays `BDO`.
4. **Import on VPS** — PH-only COPY + `ON CONFLICT`; guard `%.PS` / `market=ph`; US count still 17,719; spot-check `BDO.PS` vs US `SM`.
5. **`--update-db` / `--check-db` on 33ai** — deploy the per-market update/check code **with** or **before** import. Prove on the VPS:
   - `--check-db` shows a `ph` section (not only US).
   - `--update-db` logs Edge fetches for stale `.PS` names (and does not Yahoo `SM.PS`).
   - Manila 15:30 cron + existing US cron both invoke `python main.py --update-db`.
   - A second `--check-db` after the PH session is OK for `ph` without regressing `us`.

After that: `python main.py --backtest --market ph` and paper against 33ai history (no local Yahoo).

---

## 6. Risks

- **Edge HTML/API drift** — directory regex and chart JSON are unofficial; spike will show breakage immediately.
- **Rate limits / blocks** — 0.4s throttle; if Edge 429s, serialize to 1 worker and backoff. Do not run the 17k US Yahoo fallback against PH.
- **Holiday calendars** — if `--update-db` keeps a single NY `_last_trading_date`, every PH name looks stale on US holidays and never-stale on PH holidays. Per-market close clock is mandatory on 33ai.
- **`SM` foot-gun** — any ingest that writes `symbol='SM'` on the VPS corrupts the US tape. Import guard + `.PS` suffix.
- **`GET /api/history/symbols` size** — 17k metas already; +280 is fine. Filter by market anyway for PH clients.
- **Approximate volume** — pattern volume gates may mis-rank PSE names; ADV in pesos belongs in the scanner (`value` / Edge VALUE), not this table, until proven otherwise.

---

## 7. Explicitly not in this plan

- Live broker routing, PSETradeX, IBKR.
- Intraday (15m) history.
- Kronos finetune on PSE (gate stays off until a PH backtest).
- Replacing `philippine_stocks.md` (operator/product plan). This file is **how bars get into `stocks_history`**.
- Dumping or cloning the full US 5.86 GB database to the laptop.
- A separate “PSE-only updater” binary. Daily refresh on 33ai is `python main.py --update-db` / `--check-db` for **both** markets.
