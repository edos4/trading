"""Bulk daily-bar fetch for the paper stream.

Lives outside data/db.py, data/history.py, and data/history_client.py.
Those three files are hashed into every pinned pattern version, so editing
them makes MarketScanner refuse to start.
"""

from __future__ import annotations

import time
from typing import Any

from utils.logger import log

_BULK_CHUNK = 200


def _bar_dict(ts, bar_date, o, h, l, c, v) -> dict[str, Any]:
    return {
        "ts": int(ts),
        "date": bar_date.isoformat() if hasattr(bar_date, "isoformat") else str(bar_date),
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(c),
        "volume": int(v) if v is not None else 0,
    }


def _bars_to_tape_rows(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        ts = bar.get("ts")
        if ts is None:
            continue
        rows.append({
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume") or 0),
            "timestamp": int(ts),
        })
    return rows


def load_daily_ohlcv_rows_bulk(
    symbols: list[str],
    after_ts: int | None = None,
    limit: int | None = None,
    *,
    market: str | None = None,
) -> dict[str, list[dict[str, Any]]] | None:
    """Bars for many symbols in one query. None when Postgres is unavailable.

    Keys are the caller's symbols (BDO stays BDO when market=ph; the lookup
    uses BDO.PS). A symbol with no rows is present with [].
    """
    from data.db import _history_symbol, get_conn

    callers: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = (raw or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        callers.append(symbol)
    if not callers:
        return {}
    storage_for = {symbol: _history_symbol(symbol, market) for symbol in callers}
    wanted = sorted({storage for storage in storage_for.values() if storage})
    if not wanted:
        return {symbol: [] for symbol in callers}
    try:
        conn = get_conn()
    except Exception:
        return None
    try:
        sql = (
            "SELECT symbol, ts, bar_date, open, high, low, close, volume FROM ("
            "SELECT symbol, ts, bar_date, open, high, low, close, volume, "
            "ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn "
            "FROM daily_bars WHERE symbol = ANY(%s)"
        )
        params: list[Any] = [wanted]
        if after_ts is not None:
            sql += " AND ts > %s"
            params.append(int(after_ts))
        sql += ") q"
        if limit is not None:
            sql += " WHERE rn <= %s"
            params.append(max(1, int(limit)))
        sql += " ORDER BY symbol, ts"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            fetched = cur.fetchall()
    except Exception:
        log.exception("DB | load_daily_ohlcv_rows_bulk failed")
        return None
    finally:
        conn.close()
    by_storage: dict[str, list[dict[str, Any]]] = {storage: [] for storage in wanted}
    for symbol, ts, bar_date, o, h, l, c, v in fetched:
        by_storage.setdefault(symbol, []).append(
            _bar_dict(ts, bar_date, o, h, l, c, v)
        )
    return {
        caller: by_storage.get(storage_for[caller], [])
        for caller in callers
    }


def _post(path: str, payload: dict[str, Any]):
    from data.history_client import (
        _RETRIES,
        _client,
        _client_lock,
        _is_transport_error,
        _request_sema,
    )

    last_exc: BaseException | None = None
    for attempt in range(_RETRIES):
        _request_sema.acquire()
        try:
            with _client_lock:
                client = _client()
            return client.post(path, json=payload)
        except Exception as exc:
            last_exc = exc
            if not (_is_transport_error(exc) and attempt + 1 < _RETRIES):
                raise
        finally:
            _request_sema.release()
        time.sleep(0.4 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _fetch_history_bars_bulk_chunk(
    symbols: list[str],
    after_ts: int | None,
    limit: int | None,
    market: str | None,
) -> dict[str, list[dict[str, Any]]] | None:
    """One POST /api/history/bulk. None if that route is not deployed."""
    from data.history_client import _log_fail

    payload: dict[str, Any] = {"symbols": symbols}
    if after_ts is not None:
        payload["after_ts"] = int(after_ts)
    if limit is not None:
        payload["limit"] = int(limit)
    if market:
        payload["market"] = market
    try:
        resp = _post(path="/api/history/bulk", payload=payload)
    except Exception as exc:
        _log_fail("POST /api/history/bulk", exc)
        return None
    if resp.status_code in (404, 405):
        log.info("History API | POST /api/history/bulk is not deployed — per-symbol fetch")
        return None
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log_fail("POST /api/history/bulk", exc)
        return None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict):
        return None
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        bars = results.get(symbol)
        if bars is None:
            bars = results.get(symbol.upper())
        out[symbol] = bars if isinstance(bars, list) else []
    return out


def fetch_history_bars_bulk(
    symbols: list[str],
    after_ts: int | None = None,
    limit: int | None = None,
    *,
    market: str | None = None,
) -> dict[str, list[dict[str, Any]]] | None:
    """Bars for many symbols. None when the bulk route is missing or the first chunk fails.

    Later chunk failures return the symbols already fetched so the caller can
    fill the gaps one symbol at a time.
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        uniq.append(symbol)
    if not uniq:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(uniq), _BULK_CHUNK):
        chunk = uniq[start:start + _BULK_CHUNK]
        part = _fetch_history_bars_bulk_chunk(chunk, after_ts, limit, market)
        if part is None:
            return None if not out else out
        out.update(part)
    return out


def load_daily_tape_rows_bulk(
    symbols: list[str],
    *,
    after_ts: int | None = None,
    limit: int | None = None,
    market: str | None = None,
) -> dict[str, list[dict[str, Any]]] | None:
    """Tape rows for many symbols in one query or one bulk HTTP call.

    None means the bulk path is unavailable (old history server, or the
    database is down). The caller then falls back to per-symbol fetches.
    A present key with [] is a symbol that has no bars.
    """
    from data.history import local_history_backfill_enabled

    uniq: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        uniq.append(symbol)
    if not uniq:
        return {}
    if local_history_backfill_enabled():
        grouped = load_daily_ohlcv_rows_bulk(
            uniq, after_ts=after_ts, limit=limit, market=market,
        )
    else:
        grouped = fetch_history_bars_bulk(
            uniq, after_ts=after_ts, limit=limit, market=market,
        )
    if grouped is None:
        return None
    return {symbol: _bars_to_tape_rows(bars) for symbol, bars in grouped.items()}
