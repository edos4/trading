"""
data/ensure_history.py — Postgres ping on web start; optional Yahoo/PSE fill.

Web start only checks that stocks_history is reachable. It does not CSV-sync
or Yahoo-walk the universe (those stall /paper). CLI `run_ensure_complete()`
Yahoo/PSE-fills stale symbols when you actually want a full fetch.
"""

from __future__ import annotations

import threading

from data import db
from data.update import _fetch_symbol, _last_trading_date
from utils.logger import log

_started = False
_lock = threading.Lock()


def ping_db() -> None:
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()


def run_ensure_complete(*, fetch: bool = True) -> dict[str, int]:
    """Yahoo/PSE-fill missing/stale bars for every symbol in stocks_history.

    Returns counts: symbols, incomplete, fetched, upserted_bars.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        if not symbols:
            log.info("ensure-history | stocks_history has no symbols yet")
            return {"symbols": 0, "incomplete": 0, "fetched": 0, "upserted_bars": 0}

        last_trading = _last_trading_date()
        target_ts = int(
            datetime(
                last_trading.year, last_trading.month, last_trading.day, tzinfo=ny
            ).timestamp()
        )
        incomplete = [
            s
            for s in symbols
            if (s["last_bar_ts"] or 0) < target_ts or (s["row_count"] or 0) == 0
        ]
        log.info(
            f"ensure-history | {len(incomplete)}/{len(symbols)} symbol(s) "
            f"missing or stale vs last trading day {last_trading}"
        )
        fetched = upserted = 0
        if fetch:
            for i, sym in enumerate(incomplete):
                try:
                    n = _fetch_symbol(
                        conn, sym["symbol"], sym["market"] or "us", fill_all=True
                    )
                    if n:
                        fetched += 1
                        upserted += n
                except Exception as exc:
                    log.warning(
                        f"ensure-history | fetch failed for {sym['symbol']}: {exc}"
                    )
                if (i + 1) % 50 == 0:
                    conn.commit()
                    log.info(
                        f"ensure-history | progress {i + 1}/{len(incomplete)}"
                    )
            conn.commit()

        log.info(
            f"ensure-history | done: fetched {fetched} symbol(s), "
            f"upserted {upserted} bar(s)"
        )
        return {
            "symbols": len(symbols),
            "incomplete": len(incomplete),
            "fetched": fetched,
            "upserted_bars": upserted,
        }
    finally:
        conn.close()


def start_web_history_backfill() -> None:
    """Ping Postgres so --web fails fast if stocks_history is down. No sync."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    try:
        ping_db()
        log.info("Web UI | PostgreSQL connected (stocks_history)")
    except Exception:
        log.exception(
            "Web UI | PostgreSQL not reachable — history ping skipped"
        )
