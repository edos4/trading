"""
data/ensure_history.py — make stocks_history current when the web UI starts.

For every symbol already in the database:
  1. CSV sync if the local stocks_data tree exists.
  2. If last bar is before the last US trading day (or row_count is 0),
     download full Yahoo/PSE daily history and upsert so internal gaps fill.

Runs in a daemon thread from `web.app.run()` so uvicorn still binds quickly.
"""

from __future__ import annotations

import threading
from pathlib import Path

from data import db
from data.update import _csv_sync, _fetch_symbol, _last_trading_date
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


def run_ensure_complete(
    data_dir: str | Path | None = None,
    *,
    fetch: bool = True,
) -> dict[str, int]:
    """Fill missing/stale bars for every symbol in stocks_history.

    Returns counts: symbols, incomplete, fetched, upserted_bars.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    data_dir = Path(data_dir or "/home/r00t/stocks_data")
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        if not symbols:
            log.info("ensure-history | stocks_history has no symbols yet")
            return {"symbols": 0, "incomplete": 0, "fetched": 0, "upserted_bars": 0}

        if data_dir.is_dir():
            n_sync, sync_bars = _csv_sync(conn, data_dir, symbols)
            log.info(
                f"ensure-history | CSV sync {n_sync} file(s), {sync_bars} new bar(s)"
            )
            symbols = db.all_symbols(conn)

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


def start_web_history_backfill(data_dir: str | Path | None = None) -> None:
    """Ping Postgres synchronously; fill missing history in a daemon thread."""
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
            "Web UI | PostgreSQL not reachable — history backfill skipped"
        )
        return

    def _worker() -> None:
        try:
            run_ensure_complete(data_dir)
        except Exception:
            log.exception("Web UI | stocks_history backfill failed")

    threading.Thread(
        target=_worker, name="stocks-history-backfill", daemon=True
    ).start()
