"""HTTP client for the VPS stocks_history API (GET /api/history)."""

from __future__ import annotations

from typing import Any

from config import settings
from utils.logger import log

_TIMEOUT = 60.0


def _base_url() -> str:
    return (settings.stocks_history_url or "").strip().rstrip("/")


def history_api_configured() -> bool:
    return bool(_base_url())


def _client():
    import httpx

    user, password = settings.stocks_history_auth
    return httpx.Client(
        base_url=_base_url(),
        auth=(user, password) if user or password else None,
        timeout=_TIMEOUT,
        headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
    )


def fetch_history_symbols() -> list[dict[str, Any]] | None:
    if not history_api_configured():
        return None
    try:
        with _client() as client:
            resp = client.get("/api/history/symbols")
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
        with _client() as client:
            resp = client.get(f"/api/history/{symbol}", params=params)
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
        with _client() as client:
            resp = client.get(f"/api/history/{symbol}/meta")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception(f"History API | GET /api/history/{symbol}/meta failed")
        return None
    return data if isinstance(data, dict) else None
