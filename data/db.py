"""
data/db.py — PostgreSQL access for the stock-history database.

One shared code path for `--ingest-db`, `--check-db`, and `--update-db` so
the scanner (and any future reader) never has to re-parse the ~3.7 GB of
CSVs under /home/r00t/stocks_data.

Connection is a local unix socket with peer auth (OS user r00t → role r00t),
so no password is involved. `settings.database_url` is the DSN; when it is
empty the discrete `db_*` fields are assembled into one.

Schema:
  daily_bars(symbol, ts, bar_date, open, high, low, close, volume)
      PK (symbol, ts); bar_date derived from ts in America/New_York.
  symbols(symbol, letter, market, source_path, last_bar_ts, row_count,
      file_mtime, file_size, updated_at)

`ts` (Unix seconds) is the file's identity; `bar_date` is reporting-only.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import settings
from utils.logger import log

_NY_TZ = "America/New_York"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol   TEXT NOT NULL,
    ts       BIGINT NOT NULL,
    bar_date DATE NOT NULL,
    open     DOUBLE PRECISION NOT NULL,
    high     DOUBLE PRECISION NOT NULL,
    low      DOUBLE PRECISION NOT NULL,
    close    DOUBLE PRECISION NOT NULL,
    volume   BIGINT NOT NULL,
    PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS daily_bars_bar_date_idx ON daily_bars (bar_date);

CREATE TABLE IF NOT EXISTS symbols (
    symbol      TEXT PRIMARY KEY,
    letter      TEXT NOT NULL,
    market      TEXT NOT NULL DEFAULT 'us',
    source_path TEXT,
    last_bar_ts BIGINT,
    row_count   BIGINT,
    file_mtime  BIGINT,
    file_size   BIGINT,
    updated_at  TIMESTAMPTZ
);
"""

# Staging table used by copy_bars: columns only, no PK, so COPY can load
# duplicates and the INSERT ... ON CONFLICT below resolves them.
_STAGE_SQL = """
CREATE TEMP TABLE IF NOT EXISTS _stage (
    symbol TEXT,
    ts     BIGINT,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION,
    volume BIGINT
)
"""

# bar_date is derived in SQL so callers never pass it: rows are
# (symbol, ts, open, high, low, close, volume).
_UPSERT_SQL = """
INSERT INTO daily_bars (symbol, ts, bar_date, open, high, low, close, volume)
VALUES (%s, %s, (to_timestamp(%s) AT TIME ZONE 'America/New_York')::date,
        %s, %s, %s, %s, %s)
ON CONFLICT (symbol, ts) DO UPDATE SET
    bar_date = EXCLUDED.bar_date,
    open     = EXCLUDED.open,
    high     = EXCLUDED.high,
    low      = EXCLUDED.low,
    close    = EXCLUDED.close,
    volume   = EXCLUDED.volume
"""

_STAGE_COPY_SQL = "COPY _stage (symbol, ts, open, high, low, close, volume) FROM STDIN"

_STAGE_FLUSH_SQL = """
INSERT INTO daily_bars (symbol, ts, bar_date, open, high, low, close, volume)
SELECT symbol, ts, (to_timestamp(ts) AT TIME ZONE 'America/New_York')::date,
       open, high, low, close, volume
FROM _stage
ON CONFLICT (symbol, ts) DO UPDATE SET
    bar_date = EXCLUDED.bar_date,
    open     = EXCLUDED.open,
    high     = EXCLUDED.high,
    low      = EXCLUDED.low,
    close    = EXCLUDED.close,
    volume   = EXCLUDED.volume
"""

_SYMBOL_UPSERT_SQL = """
INSERT INTO symbols (symbol, letter, market, source_path, last_bar_ts,
                     row_count, file_mtime, file_size, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (symbol) DO UPDATE SET
    letter      = EXCLUDED.letter,
    market      = EXCLUDED.market,
    source_path = EXCLUDED.source_path,
    last_bar_ts = EXCLUDED.last_bar_ts,
    row_count   = EXCLUDED.row_count,
    file_mtime  = EXCLUDED.file_mtime,
    file_size   = EXCLUDED.file_size,
    updated_at  = now()
"""


def build_dsn() -> str:
    if settings.database_url:
        return settings.database_url
    user = settings.db_user
    if settings.db_password:
        user = f"{user}:{settings.db_password}"
    params = []
    if settings.db_host:
        params.append(f"host={settings.db_host}")
    if settings.db_port:
        params.append(f"port={settings.db_port}")
    dsn = f"postgresql://{user}@{settings.db_name}"
    if params:
        dsn += "?" + "&".join(params)
    return dsn


def get_conn():
    import psycopg

    return psycopg.connect(build_dsn())


def ensure_schema(conn=None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
    finally:
        if own:
            conn.close()


def create_staging(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_SQL)


def upsert_bars(conn, symbol: str, rows: Sequence[tuple]) -> None:
    """Upsert (ts, open, high, low, close, volume) tuples for `symbol`."""
    if not rows:
        return
    params = [(symbol, ts, ts, o, h, l, c, v) for ts, o, h, l, c, v in rows]
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, params)


def copy_bars(conn, symbol: str, rows: Iterable[tuple]) -> None:
    """Bulk-load (ts, open, high, low, close, volume) tuples via COPY.

    Rows land in the `_stage` temp table (created by `create_staging`); call
    `flush_stage` to merge them into daily_bars with ON CONFLICT.
    """
    with conn.cursor() as cur:
        with cur.copy(_STAGE_COPY_SQL) as copy:
            for ts, o, h, l, c, v in rows:
                copy.write_row((symbol, ts, o, h, l, c, v))


def flush_stage(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_FLUSH_SQL)
        cur.execute("TRUNCATE _stage")


def upsert_symbol(conn, symbol: str, letter: str, market: str, source_path: str,
                  last_bar_ts: int | None, row_count: int,
                  file_mtime: int | None, file_size: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _SYMBOL_UPSERT_SQL,
            (symbol, letter, market, source_path, last_bar_ts, row_count,
             file_mtime, file_size),
        )


def max_ts(conn, symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(ts) FROM daily_bars WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None


def refresh_symbol_meta(conn, symbol: str) -> None:
    """Recompute last_bar_ts / row_count / updated_at for one symbol."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE symbols SET "
            "last_bar_ts = (SELECT max(ts) FROM daily_bars WHERE symbol = %s), "
            "row_count = (SELECT count(*) FROM daily_bars WHERE symbol = %s), "
            "updated_at = now() WHERE symbol = %s",
            (symbol, symbol, symbol),
        )


def all_symbols(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, letter, market, source_path, last_bar_ts, row_count, "
            "file_mtime, file_size, updated_at FROM symbols ORDER BY symbol"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def median_last_bar_date(conn) -> date | None:
    """Median `symbols.last_bar_ts` as an America/New_York session date.

    Median (not max) so a couple of test/outlier tickers cannot hide a stale
    universe, and a long tail of dead tickers cannot hide a current one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (to_timestamp("
            "percentile_disc(0.5) WITHIN GROUP (ORDER BY last_bar_ts)"
            ") AT TIME ZONE %s)::date "
            "FROM symbols WHERE last_bar_ts IS NOT NULL",
            (_NY_TZ,),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return row[0]


def global_stats(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT symbol) AS n_symbols, "
            "count(*) AS n_bars, min(bar_date) AS min_date, max(bar_date) AS max_date, "
            "current_database() AS db_name "
            "FROM daily_bars"
        )
        cols = [d.name for d in cur.description]
        out = dict(zip(cols, cur.fetchone()))
        cur.execute("SELECT pg_database_size(current_database())")
        out["db_size_bytes"] = cur.fetchone()[0]
    return out


def per_symbol_range(conn) -> dict[str, tuple[date, date, int]]:
    """symbol -> (min bar_date, max bar_date, row_count)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, min(bar_date), max(bar_date), count(*) "
            "FROM daily_bars GROUP BY symbol"
        )
        return {
            symbol: (min_d, max_d, n)
            for symbol, min_d, max_d, n in cur.fetchall()
        }


def today_date() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo(_NY_TZ)).date()


def load_daily_ohlcv_rows(
    symbol: str, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Raw daily bars for the history API. Empty list if DB/symbol missing."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return []
    try:
        conn = get_conn()
    except Exception:
        return []
    try:
        sql = (
            "SELECT ts, bar_date, open, high, low, close, volume "
            "FROM daily_bars WHERE symbol = %s"
        )
        params: list[Any] = [symbol]
        if after_ts is not None:
            sql += " AND ts > %s"
            params.append(int(after_ts))
        if limit is not None:
            lim = max(1, int(limit))
            sql += " ORDER BY ts DESC LIMIT %s"
            params.append(lim)
            sql = f"SELECT * FROM ({sql}) AS recent ORDER BY ts"
        else:
            sql += " ORDER BY ts"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for ts, bar_date, o, h, l, c, v in rows:
            out.append({
                "ts": int(ts),
                "date": bar_date.isoformat() if hasattr(bar_date, "isoformat") else str(bar_date),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": int(v) if v is not None else 0,
            })
        return out
    except Exception:
        log.exception(f"DB | load_daily_ohlcv_rows failed for {symbol}")
        return []
    finally:
        conn.close()


def load_symbol_meta(symbol: str) -> dict[str, Any] | None:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    try:
        conn = get_conn()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, market, last_bar_ts, row_count FROM symbols "
                "WHERE symbol = %s",
                (symbol,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "symbol": row[0],
            "market": row[1] or "us",
            "last_bar_ts": row[2],
            "row_count": int(row[3] or 0),
        }
    except Exception:
        log.exception(f"DB | load_symbol_meta failed for {symbol}")
        return None
    finally:
        conn.close()


def load_daily_ohlcv_df(symbol: str):
    """Pandas OHLCV frame for on-demand charts. None if DB/symbol missing."""
    try:
        import pandas as pd
    except ImportError:
        return None
    try:
        conn = get_conn()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bar_date, open, high, low, close, volume "
                "FROM daily_bars WHERE symbol = %s ORDER BY ts",
                (symbol.upper(),),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        df = pd.DataFrame(
            rows, columns=["bar_date", "open", "high", "low", "close", "volume"]
        )
        df.index = pd.to_datetime(df["bar_date"])
        return df[["open", "high", "low", "close", "volume"]]
    except Exception:
        log.exception(f"DB | load_daily_ohlcv_df failed for {symbol}")
        return None
    finally:
        conn.close()


def bar_date_from_ts(ts: int) -> date:
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ts, tz=ZoneInfo(_NY_TZ)).date()


def symbol_path_for(data_dir: Path, symbol: str) -> Path:
    return data_dir / symbol[0].upper() / f"{symbol.upper()}.csv"


log.debug("data.db | database_url=%s", "set" if settings.database_url else "unset")
