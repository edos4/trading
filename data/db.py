"""
data/db.py — PostgreSQL access for the stock-history database.

`--check-db` and `--update-db` keep daily_bars current from Yahoo/PSE.
`--update-hourly` keeps hourly_bars current from Alpaca.
The scanner and UIs read via the history facade (API or this Postgres).
CSV files are not a history source.

Connection is a local unix socket with peer auth (OS user r00t → role r00t),
so no password is involved. `settings.database_url` is the DSN; when it is
empty the discrete `db_*` fields are assembled into one.

Schema:
  daily_bars(symbol, ts, bar_date, open, high, low, close, volume)
      PK (symbol, ts); bar_date derived from ts in America/New_York.
  hourly_bars(symbol, ts, bar_date, open, high, low, close, volume)
      PK (symbol, ts); bar_date derived from ts in America/New_York.
  symbols(symbol, letter, market, source_path, last_bar_ts, row_count,
      file_mtime, file_size, updated_at, last_hourly_bar_ts)

`ts` (Unix seconds) is the bar identity; `bar_date` is reporting-only.
source_path / file_mtime / file_size are leftover columns (unused).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Sequence

from config import settings
from utils.logger import log

_NY_TZ = "America/New_York"
_MANILA_TZ = "Asia/Manila"


def bar_date_tz(market: str | None) -> str:
    return _MANILA_TZ if (market or "us").lower() == "ph" else _NY_TZ

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

CREATE TABLE IF NOT EXISTS hourly_bars (
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

CREATE INDEX IF NOT EXISTS hourly_bars_bar_date_idx ON hourly_bars (bar_date);

CREATE TABLE IF NOT EXISTS symbols (
    symbol           TEXT PRIMARY KEY,
    letter           TEXT NOT NULL,
    market           TEXT NOT NULL DEFAULT 'us',
    source_path      TEXT,
    last_bar_ts      BIGINT,
    row_count        BIGINT,
    file_mtime       BIGINT,
    file_size        BIGINT,
    updated_at       TIMESTAMPTZ,
    last_hourly_bar_ts BIGINT
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
VALUES (%s, %s, (to_timestamp(%s) AT TIME ZONE %s)::date,
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
SELECT symbol, ts, (to_timestamp(ts) AT TIME ZONE %s)::date,
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
                     row_count, file_mtime, file_size, updated_at, last_hourly_bar_ts)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
ON CONFLICT (symbol) DO UPDATE SET
    letter           = EXCLUDED.letter,
    market           = EXCLUDED.market,
    source_path      = EXCLUDED.source_path,
    last_bar_ts      = EXCLUDED.last_bar_ts,
    row_count        = EXCLUDED.row_count,
    file_mtime       = EXCLUDED.file_mtime,
    file_size        = EXCLUDED.file_size,
    updated_at       = now(),
    last_hourly_bar_ts = COALESCE(EXCLUDED.last_hourly_bar_ts, symbols.last_hourly_bar_ts)
"""

# Hourly bars: separate staging to avoid mixing with daily bars.
_HOURLY_STAGE_SQL = """
CREATE TEMP TABLE IF NOT EXISTS _stage_hourly (
    symbol TEXT,
    ts     BIGINT,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION,
    volume BIGINT
)
"""

_HOURLY_STAGE_COPY_SQL = "COPY _stage_hourly (symbol, ts, open, high, low, close, volume) FROM STDIN"

_HOURLY_STAGE_FLUSH_SQL = """
INSERT INTO hourly_bars (symbol, ts, bar_date, open, high, low, close, volume)
SELECT symbol, ts, (to_timestamp(ts) AT TIME ZONE %s)::date,
       open, high, low, close, volume
FROM _stage_hourly
ON CONFLICT (symbol, ts) DO NOTHING
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
            # Only ALTER when the column is missing: an unconditional ALTER
            # takes ACCESS EXCLUSIVE on symbols and queues behind any open
            # reader (e.g. the daily updater's symbol list), stalling both runs.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'symbols' "
                "AND column_name = 'last_hourly_bar_ts'"
            )
            if cur.fetchone() is None:
                cur.execute(
                    "ALTER TABLE symbols ADD COLUMN last_hourly_bar_ts BIGINT"
                )
        conn.commit()
    finally:
        if own:
            conn.close()


def create_staging(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_SQL)


def upsert_bars(conn, symbol: str, rows: Sequence[tuple], *, market: str = "us") -> None:
    """Upsert (ts, open, high, low, close, volume) tuples for `symbol`."""
    if not rows:
        return
    tz = bar_date_tz(market)
    params = [(symbol, ts, ts, tz, o, h, l, c, v) for ts, o, h, l, c, v in rows]
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


def flush_stage(conn, *, market: str = "us") -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_FLUSH_SQL, (bar_date_tz(market),))
        cur.execute("TRUNCATE _stage")


# --- Hourly bar staging (separate from daily) ---

def create_hourly_staging(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_HOURLY_STAGE_SQL)


def copy_hourly_bars(conn, symbol: str, rows: Iterable[tuple]) -> None:
    """Bulk-load hourly bars into _stage_hourly temp table."""
    with conn.cursor() as cur:
        with cur.copy(_HOURLY_STAGE_COPY_SQL) as copy:
            for ts, o, h, l, c, v in rows:
                copy.write_row((symbol, ts, o, h, l, c, v))


def flush_hourly_stage(conn, *, market: str = "us") -> int:
    """Merge staged hourly bars, keeping rows that already exist.

    Returns the number of newly inserted bars; conflicts are skipped so a
    provider switch (Polygon → Alpaca) never rewrites existing history.
    """
    with conn.cursor() as cur:
        cur.execute(_HOURLY_STAGE_FLUSH_SQL, (bar_date_tz(market),))
        inserted = cur.rowcount
        cur.execute("TRUNCATE _stage_hourly")
    return inserted


def hourly_coverage(conn) -> dict[str, tuple[int, int]]:
    """symbol -> (first_ts, last_ts) for hourly bars already in the database."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, min(ts), max(ts) FROM hourly_bars GROUP BY symbol"
        )
        return {
            symbol: (int(first_ts), int(last_ts))
            for symbol, first_ts, last_ts in cur.fetchall()
            if first_ts is not None
        }


_LAST_HOURLY_TS_SQL = """
UPDATE symbols
SET last_hourly_bar_ts = GREATEST(COALESCE(last_hourly_bar_ts, 0), %s),
    updated_at = now()
WHERE symbol = %s
"""


def set_last_hourly_bar_ts(conn, symbol: str, ts: int) -> None:
    """Advance the hourly resume point without touching daily-bar meta."""
    with conn.cursor() as cur:
        cur.execute(_LAST_HOURLY_TS_SQL, (ts, symbol))


def upsert_symbol(conn, symbol: str, letter: str, market: str, source_path: str,
                  last_bar_ts: int | None, row_count: int,
                  file_mtime: int | None, file_size: int | None,
                  last_hourly_bar_ts: int | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _SYMBOL_UPSERT_SQL,
            (symbol, letter, market, source_path, last_bar_ts, row_count,
             file_mtime, file_size, last_hourly_bar_ts),
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


def all_symbols(conn, market: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT symbol, letter, market, source_path, last_bar_ts, row_count, "
        "file_mtime, file_size, updated_at, last_hourly_bar_ts FROM symbols"
    )
    params: list[Any] = []
    if market:
        sql += " WHERE market = %s"
        params.append(market)
    sql += " ORDER BY symbol"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def median_last_bar_date(conn, market: str | None = None) -> date | None:
    """Median `symbols.last_bar_ts` as a session date in that market's TZ."""
    tz = bar_date_tz(market) if market else _NY_TZ
    sql = (
        "SELECT (to_timestamp("
        "percentile_disc(0.5) WITHIN GROUP (ORDER BY last_bar_ts)"
        ") AT TIME ZONE %s)::date "
        "FROM symbols WHERE last_bar_ts IS NOT NULL"
    )
    params: list[Any] = [tz]
    if market:
        sql += " AND market = %s"
        params.append(market)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return row[0]


def global_stats(conn, market: str | None = None) -> dict[str, Any]:
    join = ""
    where = ""
    params: list[Any] = []
    if market:
        join = " JOIN symbols s ON s.symbol = daily_bars.symbol"
        where = " WHERE s.market = %s"
        params.append(market)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT daily_bars.symbol) AS n_symbols, "
            "count(*) AS n_bars, min(bar_date) AS min_date, max(bar_date) AS max_date, "
            "current_database() AS db_name "
            f"FROM daily_bars{join}{where}",
            params,
        )
        cols = [d.name for d in cur.description]
        out = dict(zip(cols, cur.fetchone()))
        cur.execute("SELECT pg_database_size(current_database())")
        out["db_size_bytes"] = cur.fetchone()[0]
        out["market"] = market or "all"
    return out


def per_symbol_range(
    conn, market: str | None = None,
) -> dict[str, tuple[date, date, int, str]]:
    """symbol -> (min bar_date, max bar_date, row_count, market)."""
    sql = (
        "SELECT d.symbol, min(d.bar_date), max(d.bar_date), count(*), "
        "COALESCE(s.market, 'us') "
        "FROM daily_bars d LEFT JOIN symbols s ON s.symbol = d.symbol"
    )
    params: list[Any] = []
    if market:
        sql += " WHERE s.market = %s"
        params.append(market)
    sql += " GROUP BY d.symbol, s.market"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {
            symbol: (min_d, max_d, n, mkt)
            for symbol, min_d, max_d, n, mkt in cur.fetchall()
        }


def today_date(market: str | None = None) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo(bar_date_tz(market))).date()


def _history_symbol(symbol: str, market: str | None) -> str:
    from core.market import ph_history_symbol, resolve_market_id

    symbol = (symbol or "").upper().strip()
    if market and resolve_market_id(market) == "ph":
        return ph_history_symbol(symbol)
    return symbol


def load_daily_ohlcv_rows(
    symbol: str,
    after_ts: int | None = None,
    limit: int | None = None,
    *,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Raw daily bars for the history API. Empty list if DB/symbol missing."""
    symbol = _history_symbol(symbol, market)
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


def load_hourly_ohlcv_rows(
    symbol: str,
    after_ts: int | None = None,
    limit: int | None = None,
    *,
    market: str | None = None,
) -> list[dict[str, Any]]:
    """Raw hourly bars for the history API. Empty list if DB/symbol missing."""
    symbol = _history_symbol(symbol, market)
    if not symbol:
        return []
    try:
        conn = get_conn()
    except Exception:
        return []
    try:
        sql = (
            "SELECT ts, bar_date, open, high, low, close, volume "
            "FROM hourly_bars WHERE symbol = %s"
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
        log.exception(f"DB | load_hourly_ohlcv_rows failed for {symbol}")
        return []
    finally:
        conn.close()


def load_symbol_meta(symbol: str, *, market: str | None = None) -> dict[str, Any] | None:
    symbol = _history_symbol(symbol, market)
    if not symbol:
        return None
    try:
        conn = get_conn()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, market, last_bar_ts, row_count, last_hourly_bar_ts "
                "FROM symbols "
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
            "last_hourly_bar_ts": row[4],
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


def bar_date_from_ts(ts: int, market: str | None = None) -> date:
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ts, tz=ZoneInfo(bar_date_tz(market))).date()


log.debug("data.db | database_url=%s", "set" if settings.database_url else "unset")
