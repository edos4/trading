"""
data/update_hourly.py — Populate hourly OHLCV bars via Alpaca Markets.

`python main.py --update-hourly` runs this module.

Keeps hourly_bars current for every US symbol in the symbols table. Symbols are
grouped into batches; each batch is one paginated Alpaca Bars request (10 000
data points per page), then written with COPY + one upsert transaction.

Free Basic plan: 200 requests/min, SIP history since 2016, latest 15 minutes
restricted. Extended-hours bars are included, matching the rows already loaded.

Idempotent and non-destructive: existing hourly_bars rows are authoritative.
Each run reads per-symbol coverage straight from the table, fetches only the
missing tail, and inserts with ON CONFLICT DO NOTHING — so history populated
by Polygon is never re-fetched or rewritten, only gaps are filled.

Schema:
  hourly_bars(symbol, ts, bar_date, open, high, low, close, volume)
      PK (symbol, ts); bar_date derived from ts in America/New_York.

Stamp: logs/hourly_bars_updated.txt holds the last US session ingested.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import time

from tqdm import tqdm

from config import settings
from data import db
from data.alpaca_client import AlpacaAuthError, AlpacaBadRequest, AlpacaClient
from data.db import get_conn
from utils.logger import log

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOURLY_STAMP_NAME = "hourly_bars_updated.txt"
_FETCH_WORKERS = max(1, settings.hourly_fetch_workers)
_BATCH_SIZE = max(1, settings.hourly_fetch_batch_size)
_NY_TZ = ZoneInfo("America/New_York")


def _last_trading_date(now: datetime | None = None) -> date:
    """Most recent closed US cash session."""
    from core.market import last_closed_session_date
    return last_closed_session_date("us", now)


def _read_stamp() -> date | None:
    """Read the last ingested session date from stamp file."""
    path = _REPO_ROOT / "logs" / _HOURLY_STAMP_NAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return date.fromisoformat(text.split()[0])
    except (ValueError, OSError):
        return None


def _write_stamp(day: date) -> None:
    """Atomically write last ingested session date."""
    path = _REPO_ROOT / "logs" / _HOURLY_STAMP_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{day.isoformat()}\n", encoding="utf-8")
    tmp.replace(path)


def _default_start_date() -> str:
    """Backfill window for symbols with no hourly bars yet (4 years)."""
    stamp = _read_stamp()
    return (stamp - timedelta(days=365 * 4)).isoformat() if stamp else "2022-01-01"


def _session_end_iso(day: date) -> str:
    """End of `day` on the New York clock, as UTC RFC-3339.

    Alpaca's date-only `end` is UTC midnight, which cuts off the final
    extended-hours bar once EST is in effect (19:00 ET = 00:00Z next day).
    """
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=_NY_TZ)
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbol_start(sym: dict, default_start: str) -> str:
    """First missing session for one symbol (one-day overlap for safety)."""
    last_ts = sym.get("last_hourly_bar_ts")
    if not last_ts:
        return default_start
    last_bar_date = db.bar_date_from_ts(last_ts, market="us")
    return (last_bar_date - timedelta(days=1)).isoformat()


def _alpaca_symbol(symbol: str, assets: dict[str, str]) -> str | None:
    """Map a DB ticker to Alpaca's notation (class shares use dots)."""
    for candidate in (symbol, symbol.replace("-", ".")):
        if candidate in assets:
            return candidate
    return None


def _batch_start(batch: list[tuple[str, str, str]]) -> str:
    return min(item[0] for item in batch)


def _fetch_batch(
    conn,
    client: AlpacaClient,
    batch: list[tuple[str, str, str]],
    end: str,
) -> tuple[int, int, int, int, bool]:
    """Fetch and store one batch of (start, db_symbol, alpaca_symbol) tuples.

    Existing (symbol, ts) rows are kept as-is; Alpaca only fills gaps.
    Returns (processed_symbols, symbols_with_bars, inserted_bars,
    existing_bars, failed).
    """
    start = _batch_start(batch)
    try:
        bars = client.fetch_bars(
            [alpaca for _, _, alpaca in batch], start=start, end=end
        )
        if not bars:
            return (len(batch), 0, 0, 0, False)

        db.create_hourly_staging(conn)
        last_ts_by_symbol: dict[str, int] = {}
        fetched_bars = 0
        for _, symbol, alpaca in batch:
            rows = bars.get(alpaca)
            if not rows:
                continue
            db.copy_hourly_bars(conn, symbol, rows)
            last_ts_by_symbol[symbol] = rows[-1][0]
            fetched_bars += len(rows)

        if not fetched_bars:
            return (len(batch), 0, 0, 0, False)

        inserted = db.flush_hourly_stage(conn, market="us")
        for symbol, last_ts in last_ts_by_symbol.items():
            db.set_last_hourly_bar_ts(conn, symbol, last_ts)
        conn.commit()
        skipped = fetched_bars - inserted
        log.debug(
            f"update_hourly | {len(last_ts_by_symbol)} symbol(s) → "
            f"{inserted} new bar(s), {skipped} already present"
        )
        return (len(batch), len(last_ts_by_symbol), inserted, skipped, False)

    except AlpacaBadRequest as exc:
        if len(batch) == 1:
            log.warning(
                f"update_hourly | Alpaca rejected {batch[0][1]}: {exc}"
            )
            return (1, 0, 0, 0, False)
        mid = len(batch) // 2
        left = _fetch_batch(conn, client, batch[:mid], end)
        right = _fetch_batch(conn, client, batch[mid:], end)
        return (
            left[0] + right[0],
            left[1] + right[1],
            left[2] + right[2],
            left[3] + right[3],
            left[4] or right[4],
        )
    except AlpacaAuthError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        log.warning(f"update_hourly | batch failed ({len(batch)} symbols): {exc}")
        return (len(batch), 0, 0, 0, True)


_PROGRESS_INTERVAL = 600  # seconds between progress writes
_PROGRESS_FILE = "docs/hourly_bars_progress.md"


def _write_progress(
    completed: int,
    remaining: int,
    total: int,
    new_bars: int,
    existing_bars: int,
    error_count: int,
    elapsed: float,
    eta_hours: float | None,
    to_date: str,
) -> None:
    """Write a markdown progress report every 10 minutes."""
    path = _REPO_ROOT / _PROGRESS_FILE
    pct = (completed / total * 100) if total > 0 else 0
    rate_sym = completed / elapsed if elapsed > 0 else 0  # symbols/sec
    rate_per_hour = rate_sym * 3600
    eta_str = (
        f"{eta_hours:.1f} hours" if eta_hours is not None else "calculating…"
    )

    lines = [
        "# Hourly Bars Fetch — Progress",
        "",
        f"**Timestamp:** {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Completed | {completed:,} / {total:,} ({pct:.1f}%) |",
        f"| Remaining | {remaining:,} |",
        f"| Bars fetched | {new_bars + existing_bars:,} |",
        f"| New bars inserted | {new_bars:,} |",
        f"| Existing bars kept | {existing_bars:,} |",
        f"| Errors | {error_count:,} |",
        f"| Elapsed | {elapsed / 60:.1f} minutes |",
        f"| Rate | {rate_per_hour:,.0f} symbols/hour |",
        f"| ETA to completion | {eta_str} |",
        f"| Fetch range | {to_date} (target) |",
        "",
        "## Detail",
        "",
        f"- **Total stale symbols:** {total:,}",
        f"- **Symbols completed:** {completed:,} ✅",
        f"- **Symbols remaining:** {remaining:,} ⏳",
        f"- **Bars fetched:** {new_bars + existing_bars:,}",
        f"- **New bars inserted:** {new_bars:,}",
        f"- **Bars already present (kept):** {existing_bars:,}",
        f"- **Errors encountered:** {error_count:,} ❌",
        f"- **Speed:** {rate_per_hour:,.0f} symbols/hour ({rate_sym:.2f} symbol/sec)",
        f"- **Estimated time left:** {eta_str}",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_update(*, fetch_limit: int | None = None) -> None:
    """Main entry point for hourly bar population."""
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        log.error(
            "update_hourly | missing Alpaca credentials. "
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
        )
        return

    conn = get_conn()
    try:
        db.ensure_schema(conn)
        symbols = db.all_symbols(conn)
        # What is actually stored wins over symbols.last_hourly_bar_ts (which
        # the old Polygon path also wrote). Existing bars are never rewritten.
        coverage = db.hourly_coverage(conn)
        conn.commit()  # close the read-only transaction before workers write
        if not symbols:
            log.error("update_hourly | database is empty — no symbols in symbols table")
            return

        client = AlpacaClient(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            feed=settings.alpaca_data_feed,
            paper=settings.alpaca_paper,
            requests_per_min=settings.alpaca_requests_per_min,
        )
        assets = client.fetch_assets()
        if not assets:
            log.error("update_hourly | could not load the Alpaca asset list — aborting")
            return

        last_session = _last_trading_date()
        default_start = _default_start_date()

        # Stale = no hourly bars yet, or the newest stored bar is before the
        # last closed session. Sorted oldest first so the worst gaps go first.
        pending: list[tuple[str, str, str]] = []
        stale_count = skipped_otc = skipped_unsupported = 0
        gap_fill = 0
        for sym in symbols:
            if (sym.get("market") or "us").lower() != "us":
                continue  # Alpaca does not cover Philippine stocks

            symbol = sym["symbol"]
            covered = coverage.get(symbol)
            last_ts = covered[1] if covered else None
            if last_ts is None:
                start = default_start
            else:
                last_bar_date = db.bar_date_from_ts(last_ts, market="us")
                if last_bar_date >= last_session:
                    continue
                start = (last_bar_date - timedelta(days=1)).isoformat()
                gap_fill += 1

            stale_count += 1
            alpaca = _alpaca_symbol(symbol, assets)
            if alpaca is None:
                skipped_unsupported += 1
                continue
            if assets[alpaca] == "OTC":
                # The free plan does not include the OTC feed (HTTP 403).
                skipped_otc += 1
                continue
            pending.append((start, symbol, alpaca))

        pending.sort(key=lambda item: (item[0], item[1]))

        log.info(
            f"update_hourly | {stale_count} stale of {len(symbols)} symbol(s); "
            f"skipped {skipped_otc} OTC (no free feed), "
            f"{skipped_unsupported} not in Alpaca"
        )
        log.info(
            f"update_hourly | {gap_fill} symbol(s) have existing bars "
            f"(fill gaps only), {stale_count - gap_fill} start empty"
        )
        if not pending:
            log.info("update_hourly | nothing Alpaca can serve is stale")
            _write_stamp(last_session)
            return

        if fetch_limit:
            pending = pending[:fetch_limit]
            log.info(f"update_hourly | capped to {len(pending)} symbols")

        batches = [
            pending[i:i + _BATCH_SIZE] for i in range(0, len(pending), _BATCH_SIZE)
        ]
        end = _session_end_iso(last_session)
        total = len(pending)
        log.info(
            f"update_hourly | fetch: {total} symbol(s) in {len(batches)} batch(es), "
            f"through {last_session}, workers={_FETCH_WORKERS}, "
            f"feed={settings.alpaca_data_feed}"
        )

        inserted_bars = existing_bars = 0
        fetched = 0
        completed = 0
        error_count = 0
        start_time = time.monotonic()
        last_progress_time = start_time

        def _one(batch: list[tuple[str, str, str]]) -> tuple[int, int, int, int, bool]:
            own = get_conn()
            try:
                return _fetch_batch(own, client, batch, end)
            finally:
                own.close()

        workers = max(1, min(_FETCH_WORKERS, len(batches)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, batch): batch for batch in batches}
            for fut in tqdm(as_completed(futs), total=len(futs),
                            desc="Fetch hourly", unit="batch"):
                try:
                    n_processed, n_symbols, n_new, n_existing, failed = fut.result()
                except AlpacaAuthError:
                    log.error(
                        "update_hourly | Alpaca rejected the credentials — aborting"
                    )
                    for pending_fut in futs:
                        pending_fut.cancel()
                    raise
                except Exception as exc:
                    n_processed, n_symbols, n_new, n_existing, failed = (
                        0, 0, 0, 0, True
                    )
                    log.warning(f"update_hourly | unexpected batch error: {exc}")

                completed += n_processed
                fetched += n_symbols
                inserted_bars += n_new
                existing_bars += n_existing
                error_count += 1 if failed else 0

                now = time.monotonic()
                elapsed = now - start_time
                remaining = max(0, total - completed)
                rate_sym = completed / elapsed if elapsed > 0 else 0
                eta_hours = (
                    (remaining / rate_sym) / 3600 if rate_sym > 0 else None
                )

                if now - last_progress_time >= _PROGRESS_INTERVAL:
                    _write_progress(
                        completed=completed,
                        remaining=remaining,
                        total=total,
                        new_bars=inserted_bars,
                        existing_bars=existing_bars,
                        error_count=error_count,
                        elapsed=elapsed,
                        eta_hours=eta_hours,
                        to_date=last_session.isoformat(),
                    )
                    log.info(
                        f"Progress: {completed}/{total} "
                        f"({completed / total * 100:.1f}%), "
                        f"{inserted_bars:,} new bars, "
                        f"{existing_bars:,} already present, "
                        f"{error_count} errors, eta {eta_hours:.1f}h"
                    )
                    last_progress_time = now

        # Final progress write
        _write_progress(
            completed=completed,
            remaining=0,
            total=total,
            new_bars=inserted_bars,
            existing_bars=existing_bars,
            error_count=error_count,
            elapsed=time.monotonic() - start_time,
            eta_hours=0.0,
            to_date=last_session.isoformat(),
        )

        log.info(
            f"update_hourly | complete: {fetched}/{total} symbol(s) with bars, "
            f"{error_count} batch error(s), {inserted_bars:,} new bars, "
            f"{existing_bars:,} already present (kept)"
        )

        # Only a full, error-free pass may advance the stamp: it anchors the
        # 4-year backfill window for symbols that have no hourly bars yet.
        if fetch_limit is None and error_count == 0:
            _write_stamp(last_session)
            log.info(f"update_hourly | wrote stamp {last_session}")
        else:
            log.info("update_hourly | stamp not advanced (partial or failed run)")

    except AlpacaAuthError:
        log.error("update_hourly | check ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    except Exception:
        log.exception("update_hourly | fatal error")
    finally:
        conn.close()


if __name__ == "__main__":
    run_update()
