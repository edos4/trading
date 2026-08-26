"""HTTP client for the VPS stocks_history API (GET /api/history)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from config import settings
from utils.logger import log

_TIMEOUT = 60.0
_cached_client = None
_cached_key: tuple[str, str, str] | None = None


def _base_url() -> str:
    return (settings.stocks_history_url or "").strip().rstrip("/")


def history_api_configured() -> bool:
    return bool(_base_url())


def _history_path(symbol: str, suffix: str = "") -> str:
    """Path-encode tickers so FLG/PU does not become an extra URL segment."""
    return f"/api/history/{quote(symbol, safe='')}{suffix}"


def _client():
    """Reuse one httpx.Client so a 500-symbol scan does not open 500 TLS sessions."""
    global _cached_client, _cached_key
    import httpx

    user, password = settings.stocks_history_auth
    key = (_base_url(), user, password)
    if _cached_client is not None and _cached_key == key:
        return _cached_client
    if _cached_client is not None:
        try:
            _cached_client.close()
        except Exception:
            pass
        _cached_client = None
    _cached_client = httpx.Client(
        base_url=key[0],
        auth=(user, password) if user or password else None,
        timeout=_TIMEOUT,
        headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
    )
    _cached_key = key
    return _cached_client


def fetch_history_symbols() -> list[dict[str, Any]] | None:
    if not history_api_configured():
        return None
    try:
        resp = _client().get("/api/history/symbols")
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("History API | GET /api/history/symbols failed")
        return None
    rows = data.get("symbols") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    return rows


def fetch_history_bars(
    symbol: str, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]] | None:
    if not history_api_configured():
        return None
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    params = {}
    if after_ts is not None:
        params["after_ts"] = int(after_ts)
    if limit is not None:
        params["limit"] = max(1, int(limit))
    try:
        resp = _client().get(_history_path(symbol), params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception(f"History API | GET /api/history/{symbol} failed")
        return None
    bars = data.get("bars") if isinstance(data, dict) else None
    if not isinstance(bars, list):
        return None
    return bars


def fetch_history_meta(symbol: str) -> dict[str, Any] | None:
    if not history_api_configured():
        return None
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    try:
        resp = _client().get(_history_path(symbol, "/meta"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception(f"History API | GET /api/history/{symbol}/meta failed")
        return None
    return data if isinstance(data, dict) else None
