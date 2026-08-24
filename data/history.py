"""Unified daily OHLCV source: remote API, then local Postgres, then TV/Yahoo."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from data.tv_client import OHLCVCandle, TVClient
from utils.logger import log

_ui_web_mode = False


def enable_ui_web_history() -> None:
    """--ui / --web: Backtester prefetch uses this facade (CLI --backtest does not)."""
    global _ui_web_mode
    _ui_web_mode = True


def ui_web_history_enabled() -> bool:
    return _ui_web_mode


def local_history_backfill_enabled() -> bool:
    from config import settings

    return not (settings.stocks_history_url or "").strip()


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


def load_daily_bars(
    symbol: str, *, after_ts: int | None = None, limit: int | None = None,
) -> list[dict[str, Any]] | None:
    """API (if URL set) or local Postgres. None on hard failure; [] if empty."""
    from data.history_client import fetch_history_bars, history_api_configured

    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    if history_api_configured():
        bars = fetch_history_bars(symbol, after_ts=after_ts, limit=limit)
        if bars is None:
            return None
        return bars
    from data.db import load_daily_ohlcv_rows

    return load_daily_ohlcv_rows(symbol, after_ts=after_ts, limit=limit)


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
    if not tv_fallback:
        return None
    tv_candles = fetch_ohlcv_candles(symbol, "1d", tv_fallback=True)
    return candles_to_df(tv_candles) if tv_candles else None


def load_daily_tape_rows(symbol: str) -> list[dict[str, Any]] | None:
    """Paper-stream tape rows: open/high/low/close/volume/timestamp (unix)."""
    bars = load_daily_bars(symbol)
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
    """Daily (or weekly resampled from daily) from API/DB, then optional TV."""
    daily = load_daily_candles(symbol)
    if daily:
        if _is_weekly(timeframe):
            weekly = resample_weekly(daily)
            if weekly:
                return weekly
        else:
            return daily
    if not tv_fallback:
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
