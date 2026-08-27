"""HTTP client for the VPS stocks_history API (GET /api/history)."""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote

from config import settings
from utils.logger import log

DEFAULT_STOCKS_HISTORY_URL = "https://33ai.edos.uk"
_CONNECT_TIMEOUT = 10.0
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
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
    _cached_key = key
    return _cached_client


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
            if _is_transport_error(exc) and attempt + 1 < _RETRIES:
                with _client_lock:
                    _reset_client()
                time.sleep(0.4 * (attempt + 1))
                continue
            raise
        finally:
            _request_sema.release()
    assert last_exc is not None
    raise last_exc


def _log_fail(op: str, exc: BaseException) -> None:
    if _is_transport_error(exc):
        log.warning(f"History API | {op} failed: {type(exc).__name__}: {exc}")
    else:
        log.exception(f"History API | {op} failed")


def fetch_history_symbols() -> list[dict[str, Any]] | None:
    try:
        resp = _get("/api/history/symbols")
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
    symbol: str, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]] | None:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    params = {}
    if after_ts is not None:
        params["after_ts"] = int(after_ts)
    if limit is not None:
        params["limit"] = max(1, int(limit))
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


def fetch_history_meta(symbol: str) -> dict[str, Any] | None:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    try:
        resp = _get(_history_path(symbol, "/meta"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log_fail(f"GET /api/history/{symbol}/meta", exc)
        return None
    return data if isinstance(data, dict) else None
