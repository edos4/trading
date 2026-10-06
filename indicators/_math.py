"""Shared indicator math taken from the public TradingView formulas.

Wilder ATR matches ``ta.atr`` (SMA seed, then RMA). Supertrend matches the
KivancOzbilgic band ratchet. None of this reads the chart; callers pass
OHLCV arrays oldest-first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder moving average. Same seed as TradingView ``ta.rma``."""
    src = np.asarray(values, dtype=float)
    out = np.full(src.shape, np.nan)
    if period < 1 or len(src) < period:
        return out
    out[period - 1] = np.nanmean(src[:period])
    alpha = 1.0 / period
    for i in range(period, len(src)):
        out[i] = alpha * src[i] + (1.0 - alpha) * out[i - 1]
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def wilder_atr(high, low, close, period: int) -> np.ndarray:
    return rma(true_range(high, low, close), period)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(np.asarray(values, dtype=float)).ewm(span=period, adjust=False).mean().to_numpy()


def dema(values: np.ndarray, period: int) -> np.ndarray:
    first = ema(values, period)
    return 2.0 * first - ema(first, period)


def heikin_ashi_close(open_, high, low, close) -> np.ndarray:
    """HA close. The UT Bot reel enables signals from Heikin Ashi candles."""
    o = np.asarray(open_, dtype=float)
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    return (o + h + l + c) / 4.0


@dataclass(frozen=True)
class Supertrend:
    trend: np.ndarray  # +1 bullish, -1 bearish
    line: np.ndarray   # active band
    atr: np.ndarray


def supertrend(
    high, low, close, period: int, multiplier: float, *, wilder: bool = True,
) -> Supertrend:
    """KivancOzbilgic supertrend on hl2.

    ``wilder=True`` is his "Change ATR Calculation Method" checkbox (uses
    ``ta.atr``). ``wilder=False`` uses an SMA of true range.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    tr = true_range(high, low, close)
    atr = wilder_atr(high, low, close, period) if wilder else _sma(tr, period)
    src = (high + low) / 2.0
    basic_up = src - multiplier * atr
    basic_dn = src + multiplier * atr
    final_up = np.full(n, np.nan)
    final_dn = np.full(n, np.nan)
    trend = np.ones(n)
    for i in range(n):
        if not np.isfinite(atr[i]):
            continue
        if i == 0 or not np.isfinite(final_up[i - 1]):
            final_up[i] = basic_up[i]
            final_dn[i] = basic_dn[i]
            trend[i] = 1.0
            continue
        prev_up = final_up[i - 1]
        prev_dn = final_dn[i - 1]
        final_up[i] = max(basic_up[i], prev_up) if close[i - 1] > prev_up else basic_up[i]
        final_dn[i] = min(basic_dn[i], prev_dn) if close[i - 1] < prev_dn else basic_dn[i]
        prev_trend = trend[i - 1]
        if prev_trend < 0 and close[i] > prev_dn:
            trend[i] = 1.0
        elif prev_trend > 0 and close[i] < prev_up:
            trend[i] = -1.0
        else:
            trend[i] = prev_trend
    line = np.where(trend > 0, final_up, final_dn)
    return Supertrend(trend=trend, line=line, atr=atr)


def flip_action(trend: np.ndarray) -> str | None:
    """BUY/SELL on the bar the trend changes. None when it does not."""
    if len(trend) < 2 or not np.isfinite(trend[-1]) or not np.isfinite(trend[-2]):
        return None
    if trend[-1] > 0 and trend[-2] < 0:
        return "BUY"
    if trend[-1] < 0 and trend[-2] > 0:
        return "SELL"
    return None


def _sma(values: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(values).rolling(period).mean().to_numpy()


def var_average(src: np.ndarray, length: int) -> np.ndarray:
    """Anıl Özekşi VAR. The CMO window is 9 in the published OTT script."""
    src = np.asarray(src, dtype=float)
    n = len(src)
    out = np.full(n, np.nan)
    alpha = 2.0 / (length + 1.0)
    cmo_n = 9
    up = np.zeros(n)
    down = np.zeros(n)
    up[1:] = np.maximum(src[1:] - src[:-1], 0.0)
    down[1:] = np.maximum(src[:-1] - src[1:], 0.0)
    prev = 0.0
    for i in range(n):
        if i < cmo_n:
            continue
        vud = up[i - cmo_n + 1 : i + 1].sum()
        vdd = down[i - cmo_n + 1 : i + 1].sum()
        denom = vud + vdd
        cmo = 0.0 if denom == 0 else (vud - vdd) / denom
        k = alpha * abs(cmo)
        prev = k * src[i] + (1.0 - k) * prev
        out[i] = prev
    return out


@dataclass(frozen=True)
class OttResult:
    mavg: np.ndarray
    ott: np.ndarray
    support: np.ndarray


def optimized_trend_tracker(src: np.ndarray, period: int, percent: float) -> OttResult:
    """OTT with the VAR average. Support-line cross is MAvg vs OTT lagged 2 bars."""
    src = np.asarray(src, dtype=float)
    mavg = var_average(src, period)
    n = len(src)
    long_stop = np.full(n, np.nan)
    short_stop = np.full(n, np.nan)
    ott = np.full(n, np.nan)
    direction = np.ones(n)
    for i in range(n):
        if not np.isfinite(mavg[i]):
            continue
        fark = mavg[i] * percent * 0.01
        raw_long = mavg[i] - fark
        raw_short = mavg[i] + fark
        prev_long = long_stop[i - 1] if i and np.isfinite(long_stop[i - 1]) else raw_long
        prev_short = short_stop[i - 1] if i and np.isfinite(short_stop[i - 1]) else raw_short
        long_stop[i] = max(raw_long, prev_long) if mavg[i] > prev_long else raw_long
        short_stop[i] = min(raw_short, prev_short) if mavg[i] < prev_short else raw_short
        prev_dir = direction[i - 1] if i else 1.0
        if prev_dir < 0 and mavg[i] > prev_short:
            direction[i] = 1.0
        elif prev_dir > 0 and mavg[i] < prev_long:
            direction[i] = -1.0
        else:
            direction[i] = prev_dir
        support = long_stop[i] if direction[i] > 0 else short_stop[i]
        if mavg[i] > support:
            ott[i] = support * (200.0 + percent) / 200.0
        else:
            ott[i] = support * (200.0 - percent) / 200.0
    support_line = np.where(direction > 0, long_stop, short_stop)
    return OttResult(mavg=mavg, ott=ott, support=support_line)


def support_line_cross(result: OttResult) -> str | None:
    """Published signal: MAvg crosses OTT[2]. Price/OTT cross is a separate switch."""
    mavg = result.mavg
    ott = result.ott
    if len(mavg) < 4:
        return None
    # Bar i crosses the OTT value from two bars earlier, matching plot(OTT[2]).
    if not all(np.isfinite(x) for x in (mavg[-1], mavg[-2], ott[-3], ott[-4])):
        return None
    if mavg[-1] > ott[-3] and mavg[-2] <= ott[-4]:
        return "BUY"
    if mavg[-1] < ott[-3] and mavg[-2] >= ott[-4]:
        return "SELL"
    return None


def gaussian_filter(src: np.ndarray, period: int, poles: int) -> np.ndarray:
    """Ehlers Gaussian via a pole cascade. DW's channel uses this smoother."""
    src = np.asarray(src, dtype=float)
    beta = (1.0 - np.cos(2.0 * np.pi / period)) / (1.414 ** (2.0 / poles) - 1.0)
    alpha = -beta + np.sqrt(beta * beta + 2.0 * beta)
    out = src.copy()
    for _ in range(int(poles)):
        filtered = np.empty_like(out)
        prev = out[0]
        for i in range(len(out)):
            prev = alpha * out[i] + (1.0 - alpha) * prev
            filtered[i] = prev
        out = filtered
    return out


@dataclass(frozen=True)
class GaussianChannel:
    filt: np.ndarray
    upper: np.ndarray
    lower: np.ndarray


def gaussian_channel(
    high, low, close, *, period: int = 144, poles: int = 4,
    reduced_lag: bool = True, fast_response: bool = False, multiplier: float = 1.414,
) -> GaussianChannel:
    """Gaussian Channel [DW] defaults: 4 poles, period 144, reduced lag, 1.414 TR."""
    close = np.asarray(close, dtype=float)
    base = gaussian_filter(close, period, poles)
    filt = base
    if reduced_lag:
        lagged = gaussian_filter(base, period, poles)
        filt = 2.0 * base - lagged
        if fast_response:
            lagged2 = gaussian_filter(lagged, period, poles)
            filt = filt + (lagged - lagged2)
    tr = gaussian_filter(true_range(high, low, close), period, poles)
    return GaussianChannel(filt=filt, upper=filt + multiplier * tr, lower=filt - multiplier * tr)


def slope_flip(filt: np.ndarray) -> str | None:
    if len(filt) < 3 or not np.isfinite(filt[-1]) or not np.isfinite(filt[-2]) or not np.isfinite(filt[-3]):
        return None
    up_now = filt[-1] > filt[-2]
    up_prev = filt[-2] > filt[-3]
    if up_now and not up_prev:
        return "BUY"
    if not up_now and up_prev:
        return "SELL"
    return None


def atr_trailing_stop(src: np.ndarray, atr: np.ndarray, key: float) -> np.ndarray:
    """QuantNomad UT Bot trailing stop."""
    src = np.asarray(src, dtype=float)
    atr = np.asarray(atr, dtype=float)
    n = len(src)
    stop = np.full(n, np.nan)
    for i in range(n):
        if not np.isfinite(atr[i]):
            continue
        loss = key * atr[i]
        if i == 0 or not np.isfinite(stop[i - 1]):
            stop[i] = src[i] - loss
            continue
        prev = stop[i - 1]
        if src[i] > prev and src[i - 1] > prev:
            stop[i] = max(prev, src[i] - loss)
        elif src[i] < prev and src[i - 1] < prev:
            stop[i] = min(prev, src[i] + loss)
        elif src[i] > prev:
            stop[i] = src[i] - loss
        else:
            stop[i] = src[i] + loss
    return stop


def ut_bot_flip(src: np.ndarray, stop: np.ndarray) -> str | None:
    if len(src) < 2 or not np.isfinite(stop[-1]) or not np.isfinite(stop[-2]):
        return None
    if src[-2] <= stop[-2] and src[-1] > stop[-1]:
        return "BUY"
    if src[-2] >= stop[-2] and src[-1] < stop[-1]:
        return "SELL"
    return None


@dataclass(frozen=True)
class FloorPivots:
    p: float
    r1: float
    s1: float


def floor_pivots(high: float, low: float, close: float) -> FloorPivots:
    pivot = (high + low + close) / 3.0
    return FloorPivots(p=pivot, r1=2.0 * pivot - low, s1=2.0 * pivot - high)


def kmeans3(values: np.ndarray, max_iter: int = 1000) -> tuple[np.ndarray, np.ndarray]:
    """3-cluster 1-D k-means. Returns (labels, centroids)."""
    pts = np.asarray(values, dtype=float)
    order = np.argsort(pts)
    # Spread the initial centers across the sorted performances.
    seeds = [order[0], order[len(order) // 2], order[-1]]
    centroids = pts[seeds].astype(float).copy()
    labels = np.zeros(len(pts), dtype=int)
    for _ in range(max_iter):
        dist = np.abs(pts[:, None] - centroids[None, :])
        labels = dist.argmin(axis=1)
        nxt = centroids.copy()
        for k in range(3):
            members = pts[labels == k]
            if len(members):
                nxt[k] = members.mean()
        if np.allclose(nxt, centroids):
            break
        centroids = nxt
    return labels, centroids
