"""
data/update.py — daily incremental update of the stock-history database.

`python main.py --update-db` (run after market close) keeps daily_bars current
by fetching every symbol whose DB last bar is still older than the last
trading day — Yahoo v8 chart (US) and PSE Edge (PH). Unbounded: all stale
symbols are fetched, no cap. `--update-db-fetch-limit N` can cap it;
`--update-db-no-fetch` skips the network pass.

Idempotent: the primary key (symbol, ts) makes re-runs no-ops.

Cron (vixie cron ignores CRON_TZ; poll + NY gate — see data/update_cron.py):
    TZ=America/New_York
    */15 * * * 1-5  flock -n -E 0 logs/update-db.lock sh -c \
        'cd <repo> && .venv/bin/python -m data.update_cron >> logs/db_update.log 2>&1'

Web start installs that crontab if missing and writes
logs/stocks_history_updated.txt after a successful pass.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tqdm import tqdm

from data import db
from utils.logger import log

_NY_TZ = ZoneInfo("America/New_York")
# Yahoo chart fetches; 8 keeps the 17k-symbol pass under ~1h without hammering.
_FETCH_WORKERS = 3


def _last_trading_date(now: datetime | None = None) -> date:
    """Most recent *closed* US cash session (16:00 ET).

    Before the close — and all weekend — yesterday's session is the last
    complete daily bar. Treating "today" as the target on Monday morning
    would mark every Friday-current symbol stale forever until 16:00.
    """
    now = now or datetime.now(tz=_NY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_NY_TZ)
    else:
        now = now.astimezone(_NY_TZ)
    target = now.date()
    if now.weekday() >= 5 or now.hour < 16:
        target -= timedelta(days=1)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


def _candles_to_rows(candles) -> list[tuple]:
    rows: dict[int, tuple] = {}
    for c in candles:
        if getattr(c, "timestamp", None) is None:
            continue
        ts = int(c.timestamp.timestamp())
        rows[ts] = (ts, float(c.open), float(c.high), float(c.low),
                    float(c.close), int(float(c.volume)))
    return [rows[ts] for ts in sorted(rows)]


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
             f"(older than {last_trading}, workers={_FETCH_WORKERS})")
    fetched, new_bars = 0, 0

    def _one(sym: dict) -> int:
        own = db.get_conn()
        try:
            n = _fetch_symbol(own, sym["symbol"], sym["market"] or "us")
            own.commit()
            return n
        except Exception as exc:
            own.rollback()
            log.warning(f"update | fetch failed for {sym['symbol']}: {exc}")
            return 0
        finally:
            own.close()

    workers = max(1, min(_FETCH_WORKERS, len(to_fetch)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, sym): sym["symbol"] for sym in to_fetch}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="Fetch fallback", unit="symbol"):
            try:
                n = fut.result()
            except Exception as exc:
                log.warning(f"update | fetch failed for {futs[fut]}: {exc}")
                n = 0
            if n:
                fetched += 1
                new_bars += n
    conn.commit()
    return fetched, new_bars


def run_update(
    *,
    fetch: bool = True,
    fetch_limit: int | None = None,
) -> None:
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        if not symbols:
            log.error("update | database is empty — no symbols in stocks_history")
            return

        n_fetch = fetch_bars = 0
        if fetch:
            n_fetch, fetch_bars = _fetch_fallback(conn, symbols, fetch_limit=fetch_limit)

        log.info(
            f"update | fetch: {n_fetch} symbol(s), {fetch_bars} new bar(s)"
        )
        try:
            from data.history_stamp import write_stamp

            median = db.median_last_bar_date(conn)
            if median is not None:
                write_stamp(median)
                log.info(f"update | wrote last-update stamp {median}")
        except Exception:
            log.exception("update | failed to write last-update stamp")
    finally:
        conn.close()
