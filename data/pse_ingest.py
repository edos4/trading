"""
data/pse_ingest.py — seed stocks_history with PSE Edge daily bars as TICKER.PS.

`python main.py --ingest-pse` creates the local DB if needed, crawls Edge,
and COPY-upserts PH-only rows. `--export-pse` / `--import-pse` move those
rows to 33ai without touching US tapes.
"""

from __future__ import annotations

import csv
from pathlib import Path

from core.market import is_ph_history_symbol, ph_history_symbol
from data import db
from data.pse_edge import fetch_daily_chunked, fetch_directory, _norm_symbol
from data.update import _candles_to_rows
from utils.logger import log


def ensure_database() -> None:
    import psycopg
    from psycopg.errors import DuplicateDatabase

    try:
        conn = db.get_conn()
        conn.close()
        return
    except Exception:
        pass
    admin_dsn = "postgresql://r00t@/postgres?host=/var/run/postgresql"
    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        try:
            admin.execute("CREATE DATABASE stocks_history")
            log.info("pse-ingest | created database stocks_history")
        except DuplicateDatabase:
            pass
    finally:
        admin.close()


def _letter(bare: str) -> str:
    return (bare[:1] or "?").upper()


def ingest_symbol(conn, bare: str, *, start_year: int = 2010) -> int:
    db_sym = ph_history_symbol(bare)
    db.upsert_symbol(
        conn, db_sym, _letter(bare), "ph", None, None, 0, None, None,
    )
    candles = fetch_daily_chunked(bare, start_year=start_year)
    rows = _candles_to_rows(candles)
    if rows:
        db.create_staging(conn)
        db.copy_bars(conn, db_sym, rows)
        db.flush_stage(conn, market="ph")
    db.refresh_symbol_meta(conn, db_sym)
    return len(rows)


def run_ingest(
    *,
    symbols: list[str] | None = None,
    start_year: int = 2010,
    limit: int | None = None,
) -> dict[str, int]:
    ensure_database()
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        if symbols:
            wanted = [_norm_symbol(s) for s in symbols if _norm_symbol(s)]
            mapping = {s: fetch_directory().get(s) or () for s in wanted}
            # resolve via keyword if missing from full dir
            from data.pse_edge import resolve_ids

            tickers = []
            for s in wanted:
                if mapping.get(s) or resolve_ids(s):
                    tickers.append(s)
                else:
                    log.warning(f"pse-ingest | skip unknown {s}")
        else:
            mapping = fetch_directory(force=True)
            tickers = sorted(mapping)
        if limit is not None:
            tickers = tickers[: max(0, int(limit))]
        log.info(f"pse-ingest | {len(tickers)} ticker(s), start_year={start_year}")
        ok = bars = 0
        for i, bare in enumerate(tickers, 1):
            try:
                n = ingest_symbol(conn, bare, start_year=start_year)
                conn.commit()
                if n:
                    ok += 1
                    bars += n
                log.info(f"pse-ingest | {bare}.PS {n} bar(s) ({i}/{len(tickers)})")
            except Exception as exc:
                conn.rollback()
                log.warning(f"pse-ingest | {bare} failed: {exc}")
        return {"symbols": len(tickers), "fetched": ok, "bars": bars}
    finally:
        conn.close()


def _validate_import_symbols(rows: list[dict[str, str]]) -> None:
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        market = (row.get("market") or "").lower()
        if market != "ph" or not is_ph_history_symbol(sym):
            raise ValueError(
                f"import refused: {sym!r} market={market!r} "
                "(need market=ph and symbol ending in .PS)"
            )


def export_ph(dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    conn = db.get_conn()
    try:
        with conn.cursor() as cur, (dest / "symbols.csv").open("w", newline="") as fh:
            cur.execute(
                "SELECT symbol, letter, market, source_path, last_bar_ts, "
                "row_count, file_mtime, file_size, updated_at "
                "FROM symbols WHERE market = 'ph' ORDER BY symbol"
            )
            cols = [d.name for d in cur.description]
            w = csv.writer(fh)
            w.writerow(cols)
            for row in cur:
                w.writerow(row)
        with conn.cursor() as cur, (dest / "daily_bars.csv").open("w", newline="") as fh:
            cur.execute(
                "SELECT d.symbol, d.ts, d.bar_date, d.open, d.high, d.low, "
                "d.close, d.volume FROM daily_bars d "
                "JOIN symbols s ON s.symbol = d.symbol "
                "WHERE s.market = 'ph' ORDER BY d.symbol, d.ts"
            )
            cols = [d.name for d in cur.description]
            w = csv.writer(fh)
            w.writerow(cols)
            for row in cur:
                w.writerow(row)
        log.info(f"pse-ingest | exported PH rows to {dest}")
    finally:
        conn.close()


def import_ph(src: Path) -> None:
    src = Path(src)
    symbols_path = src / "symbols.csv"
    bars_path = src / "daily_bars.csv"
    if not symbols_path.is_file() or not bars_path.is_file():
        raise FileNotFoundError(f"{src} must contain symbols.csv and daily_bars.csv")

    with symbols_path.open(newline="") as fh:
        symbol_rows = list(csv.DictReader(fh))
    _validate_import_symbols(symbol_rows)
    with bars_path.open(newline="") as fh:
        bar_rows = list(csv.DictReader(fh))
    for row in bar_rows:
        if not is_ph_history_symbol(row.get("symbol") or ""):
            raise ValueError(
                f"import refused: daily_bars symbol {row.get('symbol')!r} "
                "must end in .PS"
            )

    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        for row in symbol_rows:
            db.upsert_symbol(
                conn,
                row["symbol"].upper(),
                row.get("letter") or "?",
                "ph",
                row.get("source_path") or None,
                int(row["last_bar_ts"]) if row.get("last_bar_ts") else None,
                int(row["row_count"]) if row.get("row_count") else 0,
                int(row["file_mtime"]) if row.get("file_mtime") else None,
                int(row["file_size"]) if row.get("file_size") else None,
            )
        tuples = []
        for row in bar_rows:
            tuples.append((
                row["symbol"].upper(),
                int(row["ts"]),
                row["bar_date"],
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(float(row["volume"])),
            ))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO daily_bars "
                "(symbol, ts, bar_date, open, high, low, close, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (symbol, ts) DO UPDATE SET "
                "bar_date = EXCLUDED.bar_date, open = EXCLUDED.open, "
                "high = EXCLUDED.high, low = EXCLUDED.low, "
                "close = EXCLUDED.close, volume = EXCLUDED.volume",
                tuples,
            )
        for row in symbol_rows:
            db.refresh_symbol_meta(conn, row["symbol"].upper())
        conn.commit()
        log.info(
            f"pse-ingest | imported {len(symbol_rows)} symbol(s), "
            f"{len(tuples)} bar(s) from {src}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
