"""
data/ensure_history.py — Postgres ping + freshness/cron on web start.

Web start must not block on a 17k-symbol Yahoo walk (that stalls /paper).
It reads `logs/stocks_history_updated.txt` when present, otherwise checks
stocks_history, runs `--update-db` in the background if stale, and ensures
the weekday after-US-close cron exists. CLI `run_ensure_complete()` still
Yahoo/PSE-fills stale symbols when you want a full foreground fetch.
"""

from __future__ import annotations

import threading
from datetime import date

from data import db
from data.history_stamp import plan_web_freshness, read_stamp, write_stamp
from data.update import _fetch_symbol, _last_trading_date
from data.update_cron import ensure_weekday_update_cron
from utils.logger import log

_CSV_DIR = "/home/r00t/stocks_data"
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


def _db_median_last_bar() -> date | None:
    conn = db.get_conn()
    try:
        return db.median_last_bar_date(conn)
    finally:
        conn.close()


def _spawn_update_db() -> None:
    thread = threading.Thread(
        target=_run_update_db,
        name="stocks-history-update",
        daemon=True,
    )
    thread.start()


def _run_update_db() -> None:
    from data.update import run_update

    try:
        run_update(_CSV_DIR)
    except Exception:
        log.exception("Web UI | background --update-db failed")


def ensure_freshness_and_cron() -> str:
    """Ensure weekday cron exists; refresh stocks_history if last US close is missing.

    Returns the freshness action: `noop`, `write_stamp`, or `update`.
    """
    try:
        ensure_weekday_update_cron()
    except Exception:
        log.exception("Web UI | failed to ensure weekday --update-db cron")

    last_trading = _last_trading_date()
    stamp = read_stamp()
    db_median = None
    if stamp is None:
        try:
            db_median = _db_median_last_bar()
        except Exception:
            log.exception("Web UI | failed to read stocks_history last-bar date")
            return "update"

    action = plan_web_freshness(stamp, db_median, last_trading)
    if stamp is not None:
        log.info(
            f"Web UI | stocks_history last-update file {stamp} "
            f"(last US session {last_trading})"
        )
    else:
        log.info(
            f"Web UI | no last-update file; stocks_history median last bar "
            f"{db_median} (last US session {last_trading})"
        )

    if action == "noop":
        log.info("Web UI | stocks_history already has last US cash close")
        return action
    if action == "write_stamp":
        write_stamp(db_median or last_trading)
        log.info(
            f"Web UI | wrote last-update file {db_median or last_trading} "
            "(DB already current)"
        )
        return action

    log.info(
        f"Web UI | stocks_history stale vs {last_trading}; "
        "starting background --update-db"
    )
    _spawn_update_db()
    return action


def start_web_history_backfill() -> None:
    """Ping Postgres, ensure daily-update cron, catch up if last US close is missing."""
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
        return
    try:
        ensure_freshness_and_cron()
    except Exception:
        log.exception("Web UI | stocks_history freshness/cron check failed")
