from __future__ import annotations

from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd

from data.edgar_client import default_client as edgar_client


def extrema(
    series: pd.Series,
    kind: Literal["high", "low"],
    lookback: int = 2,
    strict: bool = False,
) -> list[int]:
    values = series.to_numpy(dtype=float)
    found: list[int] = []
    for idx in range(lookback, len(values) - lookback):
        value = values[idx]
        neighbors = np.concatenate(
            (values[idx - lookback : idx], values[idx + 1 : idx + lookback + 1])
        )
        if kind == "high":
            valid = value > neighbors.max() if strict else value >= neighbors.max()
        else:
            valid = value < neighbors.min() if strict else value <= neighbors.min()
        if valid:
            found.append(idx)
    return found


def parabolic_fit(
    close: pd.Series,
    center: int,
    radius: int,
    tolerance: float,
) -> tuple[float, float] | None:
    start = center - radius
    stop = center + radius + 1
    if start < 0 or stop > len(close):
        return None
    x = np.arange(-radius, radius + 1, dtype=float)
    y = close.iloc[start:stop].to_numpy(dtype=float)
    if not np.isfinite(y).all() or np.any(y <= 0):
        return None
    try:
        coefficient, linear, intercept = np.polyfit(x, y, 2)
    except (np.linalg.LinAlgError, ValueError):
        return None
    fitted = coefficient * x * x + linear * x + intercept
    if np.any(fitted == 0) or not np.isfinite(fitted).all():
        return None
    within = float(np.mean(np.abs(y - fitted) / np.abs(fitted) <= tolerance))
    return float(coefficient), within


def local_rsi_extrema(
    rsi: pd.Series,
    start: int,
    stop: int,
    kind: Literal["high", "low"],
) -> list[int]:
    values = rsi.to_numpy(dtype=float)
    found: list[int] = []
    for idx in range(max(1, start), min(stop, len(values) - 1)):
        if not np.isfinite(values[idx - 1 : idx + 2]).all():
            continue
        if kind == "high" and values[idx] > values[idx - 1] and values[idx] > values[idx + 1]:
            found.append(idx)
        if kind == "low" and values[idx] < values[idx - 1] and values[idx] < values[idx + 1]:
            found.append(idx)
    return found


def weak_leg_volume(
    open_: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    start: int,
    stop: int,
    weak_side: Literal["up", "down"],
) -> bool:
    up: list[float] = []
    down: list[float] = []
    for idx in range(start, stop + 1):
        if close.iloc[idx] > open_.iloc[idx]:
            up.append(float(volume.iloc[idx]))
        elif close.iloc[idx] < open_.iloc[idx]:
            down.append(float(volume.iloc[idx]))
    if not up or not down:
        return False
    up_average = sum(up) / len(up)
    down_average = sum(down) / len(down)
    return up_average < down_average if weak_side == "up" else down_average < up_average


def as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).split(" ")[0], "%Y-%m-%d").date()


def notional_qty(notional: float, price: float) -> float:
    return round(notional / price, 4) if price > 0 else 0.0


_EARNINGS_CACHE: dict[str, list[int]] | None = None


def _earnings_cache() -> dict[str, list[int]]:
    """Offline unix-sec 8-K/2.02 dates from data/barcache/earnings_cache.json."""
    global _EARNINGS_CACHE
    if _EARNINGS_CACHE is None:
        try:
            from data.barcache import load_earnings_cache
            _EARNINGS_CACHE = load_earnings_cache()
        except Exception:
            _EARNINGS_CACHE = {}
    return _EARNINGS_CACHE


def earnings_blackout(df: pd.DataFrame, symbol: str, entry: int, bars: int) -> bool:
    """True if an earnings date falls inside [entry, entry+bars+1 day].

    Prefers the offline earnings cache (deterministic backtests); falls back to
    a live SEC EDGAR lookup only when the symbol isn't in the cache.
    """
    try:
        start = as_date(df.index[entry])
        if entry + bars < len(df):
            end = as_date(df.index[entry + bars])
        else:
            end = as_date(pd.Timestamp(start) + pd.offsets.BDay(bars))
    except Exception:
        return False

    cache = _earnings_cache()
    stamps = cache.get(symbol.upper())
    if stamps is not None:
        lo = int(pd.Timestamp(start).tz_localize("UTC").timestamp())
        hi = int(pd.Timestamp(end).tz_localize("UTC").timestamp()) + 86400
        return any(lo <= s <= hi for s in stamps)

    try:
        return edgar_client().has_earnings_in(symbol, start, end)
    except Exception:
        return False
