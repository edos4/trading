"""HTTP client for the VPS stocks_history API (GET /api/history)."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote

from config import settings
from utils.logger import log

DEFAULT_STOCKS_HISTORY_URL = "https://33ai.edos.uk"
# Connect includes TLS. 10s was firing handshake timeouts to 33ai and then
# the retry path used to close the shared Client, which killed in-flight
# requests and stamped out more handshake failures.
_CONNECT_TIMEOUT = 20.0
_READ_TIMEOUT = 30.0
_MAX_INFLIGHT = 4
_RETRIES = 3
_cached_client = None
_cached_key: tuple[str, str, str] | None = None
_client_lock = threading.Lock()
_request_sema = threading.BoundedSemaphore(_MAX_INFLIGHT)


def _base_url() -> str:
    return (settings.stocks_history_url or "").strip().rstrip("/") or DEFAULT_STOCKS_HISTORY_URL


def history_api_configured() -> bool:
    """Readers always have a URL (explicit or 33ai default). Never local Postgres."""
    return True


def _history_path(symbol: str, suffix: str = "") -> str:
    """Path-encode tickers so FLG/PU does not become an extra URL segment."""
    return f"/api/history/{quote(symbol, safe='')}{suffix}"


def _timeout():
    import httpx

    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT,
        read=_READ_TIMEOUT,
        write=_CONNECT_TIMEOUT,
        pool=_CONNECT_TIMEOUT,
    )


def _reset_client() -> None:
    global _cached_client, _cached_key
    if _cached_client is not None:
        try:
            _cached_client.close()
        except Exception:
            pass
    _cached_client = None
    _cached_key = None


def _client():
    """Reuse one httpx.Client so a 500-symbol scan does not open 500 TLS sessions."""
    global _cached_client, _cached_key
    import httpx

    user, password = settings.stocks_history_auth
    key = (_base_url(), user, password)
    if _cached_client is not None and _cached_key == key:
        return _cached_client
    _reset_client()
    _cached_client = httpx.Client(
        base_url=key[0],
        auth=(user, password) if user or password else None,
        timeout=_timeout(),
        headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )
    _cached_key = key
    return _cached_client


@contextmanager
def inflight_slots(n: int) -> Iterator[None]:
    """Temporarily raise the HTTP concurrency cap (paper-stream preload)."""
    global _request_sema
    n = max(1, int(n))
    previous = _request_sema
    _request_sema = threading.BoundedSemaphore(n)
    try:
        yield
    finally:
        _request_sema = previous


def _is_transport_error(exc: BaseException) -> bool:
    import httpx

    return isinstance(exc, (
        httpx.ConnectTimeout,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
    ))


def _get(path: str, params: dict[str, Any] | None = None):
    last_exc: BaseException | None = None
    for attempt in range(_RETRIES):
        _request_sema.acquire()
        try:
            with _client_lock:
                client = _client()
            return client.get(path, params=params or {})
        except Exception as exc:
            last_exc = exc
            if not (_is_transport_error(exc) and attempt + 1 < _RETRIES):
                raise
        finally:
            _request_sema.release()
        # Backoff after releasing the slot. Sleeping while holding it stalled
        # the other 3 inflight workers during a timeout storm.
        time.sleep(0.4 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _log_fail(op: str, exc: BaseException) -> None:
    if _is_transport_error(exc):
        log.warning(f"History API | {op} failed: {type(exc).__name__}: {exc}")
    else:
        log.exception(f"History API | {op} failed")


def fetch_history_symbols(market: str | None = None) -> list[dict[str, Any]] | None:
    params = {}
    if market:
        params["market"] = market
    try:
        resp = _get("/api/history/symbols", params=params or None)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log_fail("GET /api/history/symbols", exc)
        return None
    rows = data.get("symbols") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    return rows


def fetch_history_bars(
    symbol: str,
    after_ts: int | None = None,
    limit: int | None = None,
    *,
    market: str | None = None,
) -> list[dict[str, Any]] | None:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    if (market or "").lower() == "ph":
        from core.market import ph_history_symbol

        symbol = ph_history_symbol(symbol)
    params = {}
    if after_ts is not None:
        params["after_ts"] = int(after_ts)
    if limit is not None:
        params["limit"] = max(1, int(limit))
    if market:
        params["market"] = market
    try:
        resp = _get(_history_path(symbol), params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log_fail(f"GET /api/history/{symbol}", exc)
        return None
    bars = data.get("bars") if isinstance(data, dict) else None
    if not isinstance(bars, list):
        return None
    return bars


_BULK_CHUNK = 200


def _post(path: str, payload: dict[str, Any]):
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
    payload: dict[str, Any] = {"symbols": symbols}
    if after_ts is not None:
        payload["after_ts"] = int(after_ts)
    if limit is not None:
        payload["limit"] = int(limit)
    if market:
        payload["market"] = market
    try:
        resp = _post("/api/history/bulk", payload)
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


def fetch_history_meta(symbol: str, *, market: str | None = None) -> dict[str, Any] | None:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    if (market or "").lower() == "ph":
        from core.market import ph_history_symbol

        symbol = ph_history_symbol(symbol)
    params = {"market": market} if market else None
    try:
        resp = _get(_history_path(symbol, "/meta"), params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log_fail(f"GET /api/history/{symbol}/meta", exc)
        return None
    return data if isinstance(data, dict) else None
