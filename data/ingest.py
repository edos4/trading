"""
data/ingest.py — bulk historical ingest of the daily OHLCV CSVs into Postgres.

`python main.py --ingest-db` enumerates `<data_dir>/<LETTER>/<TICKER>.csv`,
parses each file by header name (column order varies per file), skips blank
gap rows, sorts by ts and dedupes (keep last), then bulk-loads via COPY into
a temp table and merges with ON CONFLICT (symbol, ts). Idempotent: safe to
re-run, conflicts are no-ops.
"""

from __future__ import annotations

import csv
from pathlib import Path

from tqdm import tqdm

from data import db
from utils.logger import log


def parse_csv_rows(path: Path) -> list[tuple]:
    """Parse one CSV into sorted, deduped (ts, open, high, low, close, volume).

    Header-name access handles reordered columns; blank/gap rows (halted or
    delisted days with a timestamp but no OHLCV) are skipped, not fatal.
    """
    rows: dict[int, tuple] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = int(float(row["timestamp"]))
                o = float(row["open"])
                h = float(row["high"])
                l = float(row["low"])
                c = float(row["close"])
                v = int(float(row["volume"]))
            except (ValueError, KeyError):
                continue
            rows[ts] = (ts, o, h, l, c, v)
    return [rows[ts] for ts in sorted(rows)]


def run_ingest(data_dir: str | Path, *, limit: int | None = None) -> int:
    data_dir = Path(data_dir)
    conn = db.get_conn()
    try:
        db.ensure_schema(conn)
        db.create_staging(conn)

        files = sorted(p for p in data_dir.glob("*/*.csv") if p.is_file())
        if limit is not None:
            files = files[:limit]

        total_files = len(files)
        total_rows = 0
        commit_every = 50

        for i, path in enumerate(tqdm(files, desc="Ingesting CSVs", unit="file")):
            symbol = path.stem.upper()
            letter = path.parent.name.upper()
            try:
                rows = parse_csv_rows(path)
            except Exception as exc:
                log.debug(f"ingest | skipping {path.name}: {exc}")
                continue

            if not rows:
                db.upsert_symbol(
                    conn, symbol, letter, "us", str(path), None, 0,
                    path.stat().st_mtime_ns if path.exists() else None,
                    path.stat().st_size if path.exists() else None,
                )
                continue

            db.copy_bars(conn, symbol, rows)
            db.flush_stage(conn)
            db.upsert_symbol(
                conn, symbol, letter, "us", str(path), rows[-1][0], len(rows),
                path.stat().st_mtime_ns, path.stat().st_size,
            )
            total_rows += len(rows)

            if (i + 1) % commit_every == 0:
                conn.commit()

        conn.commit()
        log.info(
            f"ingest | done: {total_files} files, {total_rows:,} bars loaded "
            f"(idempotent — re-run is a no-op)"
        )
        return total_rows
    finally:
        conn.close()
