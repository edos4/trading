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

    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        if not symbols:
            log.info("ensure-history | stocks_history has no symbols yet")
            return {"symbols": 0, "incomplete": 0, "fetched": 0, "upserted_bars": 0}

        last_us = _last_trading_date()
        last_ph = _last_trading_date(market="ph")
        ny = ZoneInfo("America/New_York")
        manila = ZoneInfo("Asia/Manila")
        target_us = int(
            datetime(last_us.year, last_us.month, last_us.day, tzinfo=ny).timestamp()
        )
        target_ph = int(
            datetime(last_ph.year, last_ph.month, last_ph.day, tzinfo=manila).timestamp()
        )
        incomplete = []
        for s in symbols:
            market = (s.get("market") or "us").lower()
            target = target_ph if market == "ph" else target_us
            if (s["last_bar_ts"] or 0) < target or (s["row_count"] or 0) == 0:
                incomplete.append(s)
        log.info(
            f"ensure-history | {len(incomplete)}/{len(symbols)} symbol(s) "
            f"missing or stale vs US {last_us} / PH {last_ph}"
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


def _db_median_last_bar(market: str | None = None) -> date | None:
    conn = db.get_conn()
    try:
        return db.median_last_bar_date(conn, market=market)
    finally:
        conn.close()


def _ph_book_action() -> str:
    """PH freshness vs Manila close. noop if no PH rows or DB unreachable."""
    try:
        from core.market import last_closed_session_date
        from data.history_stamp import plan_web_freshness, read_stamp, stamp_path

        median = _db_median_last_bar("ph")
        if median is None:
            return "noop"
        stamp = read_stamp(stamp_path("ph"))
        return plan_web_freshness(stamp, median, last_closed_session_date("ph"))
    except TypeError:
        return "noop"
    except Exception:
        log.exception("Web UI | PH stocks_history freshness check failed")
        return "noop"


def _spawn_update_db() -> None:
    """Catch up stale bars in a separate process, not inside --web.

    In-thread fetch used TVClient under ui_web_history, hairpinned
    GET /api/history through kamal-proxy onto this same uvicorn, and
    starved TLS for paper scans.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    py = root / ".venv" / "bin" / "python"
    exe = str(py) if py.is_file() else sys.executable
    log_path = root / "logs" / "db_update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as logf:
        subprocess.Popen(
            [exe, str(root / "main.py"), "--update-db"],
            cwd=str(root),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    log.info("Web UI | spawned `python main.py --update-db` (separate process)")


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
    ph_action = _ph_book_action()
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

    if ph_action == "update":
        log.info("Web UI | PH stocks_history stale vs last PSE session")
        action = "update"
    elif ph_action == "write_stamp" and action != "update":
        try:
            from data.history_stamp import write_stamp as _write

            median_ph = _db_median_last_bar("ph")
            if median_ph is not None:
                _write(median_ph, market="ph")
        except Exception:
            log.exception("Web UI | failed to write PH last-update stamp")

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
