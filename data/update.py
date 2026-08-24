"""
data/update.py — daily incremental update of the stock-history database.

`python main.py --update-db` (run after market close) keeps daily_bars current
in two stages:

1. CSV sync (primary): for each known symbol, re-read its CSV only when the
   file's mtime/size changed since last ingest, and upsert rows with
   ts > max(ts). Cheap on days nothing changed.
2. Fetch fallback (runs by default): for every symbol whose DB last bar is
   still older than the last trading day, pull fresh daily closes from the
   existing sources — Yahoo v8 chart (US) and PSE Edge (PH) — and upsert
   them. Unbounded: all stale symbols are fetched, no cap. On a stale
   universe this is a long pass over ~17k symbols; `--update-db-fetch-limit
   N` can cap it, and `--update-db-no-fetch` skips the network pass entirely
   (CSV sync only).

Both stages are idempotent: the primary key (symbol, ts) makes re-runs no-ops.

Cron example (16:30 ET ≈ after US close):
    30 16 * * 1-5  cd <repo> && .venv/bin/python main.py --update-db \
        >> logs/db_update.log 2>&1
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tqdm import tqdm

from data import db
from data.ingest import parse_csv_rows
from utils.logger import log

_NY_TZ = ZoneInfo("America/New_York")


def _last_trading_date() -> "datetime.date":
    today = datetime.now(tz=_NY_TZ).date()
    while today.weekday() >= 5:  # Sat=5, Sun=6
        today -= timedelta(days=1)
    return today


def _candles_to_rows(candles) -> list[tuple]:
    rows: dict[int, tuple] = {}
    for c in candles:
        if getattr(c, "timestamp", None) is None:
            continue
        ts = int(c.timestamp.timestamp())
        rows[ts] = (ts, float(c.open), float(c.high), float(c.low),
                    float(c.close), int(float(c.volume)))
    return [rows[ts] for ts in sorted(rows)]


def _csv_sync(conn, data_dir: Path, symbols) -> tuple[int, int]:
    updated, new_bars = 0, 0
    for sym in tqdm(symbols, desc="CSV sync", unit="symbol"):
        path = Path(sym["source_path"]) if sym["source_path"] else db.symbol_path_for(
            data_dir, sym["symbol"]
        )
        if not path.exists():
            continue
        try:
            st = path.stat()
            mtime, size = st.st_mtime_ns, st.st_size
        except OSError:
            continue
        if sym["file_mtime"] == mtime and sym["file_size"] == size:
            continue
        try:
            rows = parse_csv_rows(path)
        except Exception as exc:
            log.debug(f"update | CSV sync failed for {sym['symbol']}: {exc}")
            continue
        last_ts = sym["last_bar_ts"] or 0
        new = [r for r in rows if r[0] > last_ts]
        if new:
            db.upsert_bars(conn, sym["symbol"], new)
            new_bars += len(new)
        db.upsert_symbol(
            conn, sym["symbol"], sym["letter"], sym["market"], str(path),
            rows[-1][0] if rows else last_ts, len(rows), mtime, size,
        )
        updated += 1
    conn.commit()
    return updated, new_bars


def _fetch_symbol(conn, symbol: str, market: str, *, fill_all: bool = False) -> int:
    candles = []
    if market == "ph":
        from data.pse_edge import fetch_daily

        candles = fetch_daily(symbol)
    elif fill_all:
        from data.tv_client import fetch_yahoo_daily_max

        candles = fetch_yahoo_daily_max(symbol)
    else:
        from data.tv_client import TVClient

        tv = TVClient(screener="america", exchange="NASDAQ")
        candles = tv._fetch_history_chart(symbol, "1d")

    rows = _candles_to_rows(candles)
    if not rows:
        return 0
    if fill_all:
        db.upsert_bars(conn, symbol, rows)
        db.refresh_symbol_meta(conn, symbol)
        return len(rows)
    last_ts = db.max_ts(conn, symbol) or 0
    new = [r for r in rows if r[0] > last_ts]
    if new:
        db.upsert_bars(conn, symbol, new)
    db.refresh_symbol_meta(conn, symbol)
    return len(new)


def _fetch_fallback(conn, symbols, *, fetch_limit: int | None) -> tuple[int, int]:
    last_trading = _last_trading_date()
    target_ts = int(
        datetime(last_trading.year, last_trading.month, last_trading.day, tzinfo=_NY_TZ)
        .timestamp()
    )

    stale = [s for s in symbols if (s["last_bar_ts"] or 0) < target_ts]
    stale.sort(key=lambda s: s["last_bar_ts"] or 0, reverse=True)

    to_fetch = stale if fetch_limit is None else stale[:fetch_limit]

    if not to_fetch:
        return 0, 0

    cap_note = f" (capped to {len(to_fetch)})" if fetch_limit else ""
    log.info(f"update | fetch fallback: {len(to_fetch)} stale symbol(s){cap_note} "
             f"(older than {last_trading})")
    fetched, new_bars = 0, 0
    for i, sym in enumerate(tqdm(to_fetch, desc="Fetch fallback", unit="symbol")):
        try:
            n = _fetch_symbol(conn, sym["symbol"], sym["market"])
            if n:
                fetched += 1
                new_bars += n
        except Exception as exc:
            log.warning(f"update | fetch failed for {sym['symbol']}: {exc}")
        if (i + 1) % 250 == 0:
            conn.commit()
    conn.commit()
    return fetched, new_bars


def run_update(
    data_dir: str | Path,
    *,
    fetch: bool = True,
    fetch_limit: int | None = None,
) -> None:
    data_dir = Path(data_dir)
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        if not symbols:
            log.error("update | database is empty — run `python main.py --ingest-db` first")
            return

        n_sync, sync_bars = _csv_sync(conn, data_dir, symbols)
        n_fetch = fetch_bars = 0
        if fetch:
            n_fetch, fetch_bars = _fetch_fallback(conn, symbols, fetch_limit=fetch_limit)

        log.info(
            f"update | CSV sync: {n_sync} file(s), {sync_bars} new bar(s); "
            f"fetch fallback: {n_fetch} symbol(s), {fetch_bars} new bar(s)"
        )
    finally:
        conn.close()
