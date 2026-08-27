"""Unified daily OHLCV source: GET /api/history (33ai by default), then TV/Yahoo.

Readers never use CSV files or local Postgres. Postgres is only for
`--update-db` / `--check-db` and for serving `/api/history` on the VPS owner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data.history_client import DEFAULT_STOCKS_HISTORY_URL
from data.tv_client import OHLCVCandle, TVClient
from utils.logger import log

_VPS_APP_ROOT = "/home/deploy/apps/trading"

_ui_web_mode = False
_applied_remote_url = False


def owns_local_stocks_history() -> bool:
    """True on the VPS that owns Postgres and serves GET /api/history."""
    from config import settings

    if settings.stocks_history_owner:
        return True
    cwd = str(Path.cwd().resolve())
    if cwd == _VPS_APP_ROOT or cwd.startswith(_VPS_APP_ROOT + "/"):
        return True
    try:
        import socket

        host = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    except OSError:
        host = ""
    return "33ai.edos.uk" in host


def enable_ui_web_history() -> None:
    """--ui / --web / paper stream: facade history, no Yahoo. Laptops hit 33ai.edos.uk."""
    global _ui_web_mode
    _ui_web_mode = True
    _apply_local_remote_history_url()


def disable_ui_web_history() -> None:
    """Test helper: undo enable_ui_web_history() URL injection."""
    global _ui_web_mode, _applied_remote_url
    _ui_web_mode = False
    if _applied_remote_url:
        from config import settings

        settings.stocks_history_url = ""
        _applied_remote_url = False


def ui_web_history_enabled() -> bool:
    return _ui_web_mode


def _apply_local_remote_history_url() -> None:
    """Laptops: default STOCKS_HISTORY_URL to 33ai. Skip on the VPS owner."""
    global _applied_remote_url
    from config import settings

    current = (settings.stocks_history_url or "").strip()
    if current:
        log.info(f"History | --ui/--web using {current.rstrip('/')}")
        return
    if owns_local_stocks_history():
        log.info("History | --ui/--web on history owner — local Postgres")
        return
    settings.stocks_history_url = DEFAULT_STOCKS_HISTORY_URL
    _applied_remote_url = True
    log.info(
        "History | local --ui/--web OHLCV (including charts) from "
        f"{DEFAULT_STOCKS_HISTORY_URL}"
    )


def local_history_backfill_enabled() -> bool:
    """True only on the VPS that owns Postgres. Local --web never Yahoo-walks."""
    from config import settings

    if (settings.stocks_history_url or "").strip():
        return False
    return owns_local_stocks_history()


def bars_to_candles(bars: list[dict[str, Any]] | None) -> list[OHLCVCandle]:
    if not bars:
        return []
    candles: list[OHLCVCandle] = []
    for bar in bars:
        ts = bar.get("ts")
        stamp = None
        if ts is not None:
            try:
                stamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                stamp = None
        if stamp is None and bar.get("date"):
            try:
                stamp = datetime.fromisoformat(str(bar["date"]))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except ValueError:
                stamp = None
        candles.append(
            OHLCVCandle(
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar.get("volume") or 0),
                timestamp=stamp,
            )
        )
    return candles


def bars_to_df(bars: list[dict[str, Any]] | None) -> pd.DataFrame | None:
    candles = bars_to_candles(bars)
    if not candles:
        return None
    return candles_to_df(candles)


def candles_to_df(candles: list[OHLCVCandle]) -> pd.DataFrame | None:
    if not candles:
        return None
    idx = pd.to_datetime([c.timestamp for c in candles])
    df = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        },
        index=idx,
    )
    df.index.name = None
    return df


def resample_weekly(daily: list[OHLCVCandle]) -> list[OHLCVCandle]:
    if len(daily) < 5:
        return []
    df = candles_to_df(daily)
    if df is None:
        return []
    weekly = df.resample("W-FRI", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return [
        OHLCVCandle(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            timestamp=idx.to_pydatetime(),
        )
        for idx, row in weekly.iterrows()
    ]


def _is_weekly(timeframe: str) -> bool:
    return timeframe.upper() in ("1W", "W", "1WK", "WEEKLY")


def list_history_symbols() -> list[dict[str, Any]]:
    """Symbol metas from GET /api/history/symbols."""
    from data.history_client import fetch_history_symbols

    return fetch_history_symbols() or []


def load_daily_bars(
    symbol: str, *, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]] | None:
    """GET /api/history/{symbol}. None on hard failure; [] if empty."""
    from data.history_client import fetch_history_bars

    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    return fetch_history_bars(symbol, after_ts=after_ts, limit=limit)


def load_daily_candles(
    symbol: str, *, limit: int | None = None,
) -> list[OHLCVCandle] | None:
    bars = load_daily_bars(symbol, limit=limit)
    if bars is None:
        return None
    candles = bars_to_candles(bars)
    return candles or None


def load_daily_ohlcv_df(
    symbol: str, *, tv_fallback: bool = False, limit: int | None = None,
) -> pd.DataFrame | None:
    candles = load_daily_candles(symbol, limit=limit)
    if candles:
        return candles_to_df(candles)
    if not tv_fallback or ui_web_history_enabled():
        return None
    tv_candles = fetch_ohlcv_candles(symbol, "1d", tv_fallback=True)
    return candles_to_df(tv_candles) if tv_candles else None


def load_daily_tape_rows(
    symbol: str, *, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]] | None:
    """Paper-stream tape rows: open/high/low/close/volume/timestamp (unix)."""
    bars = load_daily_bars(symbol, after_ts=after_ts, limit=limit)
    if not bars:
        return None
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
    return rows or None


def fetch_ohlcv_candles(
    symbol: str,
    timeframe: str = "1d",
    *,
    exchange: str | None = None,
    tv_client: TVClient | None = None,
    tv_fallback: bool = True,
) -> list[OHLCVCandle]:
    """Daily (or weekly resampled from daily) from GET /api/history, then optional TV.

    --ui/--web never fall back to Yahoo/TV: charts and OHLCV come from
    33ai.edos.uk.
    """
    daily = load_daily_candles(symbol)
    if daily:
        if _is_weekly(timeframe):
            weekly = resample_weekly(daily)
            if weekly:
                return weekly
        else:
            return daily
    if not tv_fallback or ui_web_history_enabled():
        return []
    if tv_client is None:
        from core.market import get_market

        profile = get_market()
        tv_client = TVClient(profile.tv_screener, profile.tv_exchange)
    log.info(f"History | TV fallback for {symbol} {timeframe}")
    if exchange:
        return tv_client._fetch_history_screener(symbol, exchange, timeframe) or []
    return tv_client._fetch_history_chart(symbol, timeframe) or []


fetch_ohlcv_candles = fetch_ohlcv_candles
ui_web_history_enabled = ui_web_history_enabled
