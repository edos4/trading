"""DII band votes and required one-day daily bulls confirmation for BUY."""

from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine

LENGTH = 20
PERIOD = 40
MIN_BARS = LENGTH + PERIOD
LONG_MIN_BULLS = 16.0
LONG_MAX_BULLS = 40.0
SHORT_MIN_BEARS = 16.0
SHORT_MAX_BEARS = 40.0
BALANCED = 50.0
BULLS_UPTREND_DAYS = 1
BULLS_UPTREND_MIN_BARS = MIN_BARS + BULLS_UPTREND_DAYS


def describe_dii_bulls_uptrend_rejection(df) -> str | None:
    """Require one strict daily increase in bulls (two finite readings).

    The caller supplies completed daily candles only. Flat or falling days fail
    confirmation; neither price direction nor the raw ``up`` event count
    substitutes for the DII ``bulls`` percentage. Insufficient history fails closed.
    """
    if df is None or len(df) < BULLS_UPTREND_MIN_BARS:
        return (f"DII bulls uptrend unavailable: need at least "
                f"{BULLS_UPTREND_MIN_BARS} completed daily candles for BUY.")
    bulls = IndicatorEngine(df).directional_imbalance_index(LENGTH, PERIOD)["bulls"]
    values = bulls.iloc[-(BULLS_UPTREND_DAYS + 1):].to_numpy(dtype=float)
    if len(values) != BULLS_UPTREND_DAYS + 1 or not np.isfinite(values).all():
        return (f"DII bulls uptrend unavailable: need {BULLS_UPTREND_DAYS + 1} "
                "finite daily bulls readings for BUY.")
    if not (np.diff(values) > 0).all():
        readings = " → ".join(f"{value:.2f}%" for value in values)
        return (f"DII bulls must rise for at least {BULLS_UPTREND_DAYS} completed trading "
                f"day{'s' if BULLS_UPTREND_DAYS != 1 else ''} before BUY; "
                f"latest readings: {readings}.")
    return None


def dii_consideration(state) -> tuple[float, float]:
    """Latest (bulls, bears) for the DII consideration window.

    A zero-event window has no directional imbalance at all, so it is balanced
    (50/50) rather than unavailable. Missing or invalid windows stay NaN.
    """
    bulls = float(state["bulls"].iloc[-1])
    bears = float(state["bears"].iloc[-1])
    if np.isfinite(bulls):
        return bulls, bears
    events = float(state["up"].iloc[-1]) + float(state["down"].iloc[-1])
    if np.isfinite(events) and events == 0:
        return BALANCED, BALANCED
    return float("nan"), float("nan")


def describe_pattern_dii_rejection(df, *, action: str = "BUY") -> str | None:
    """Accept the DII consideration band for the signal's side.

    BUY (long) accepts 16 <= bulls <= 40; SELL (short) accepts 16 <= bears <= 40.
    Because bulls + bears == 100, those bands do not overlap: a tape with bulls
    16–40 passes only BUY, a tape with bulls 60–84 (bears 16–40) passes only
    SELL, and the 40–60 middle (e.g. a balanced 50/50 tape) passes neither.
    A short warmup window (e.g. the first PATTERN_SCAN_HISTORY_BARS bars after
    a process start, while bulk history is still preloading) carries no
    rejection — the gate engages once enough candles are stored. A zero-event
    window is balanced, not unavailable.
    """
    if df is None or len(df) < MIN_BARS:
        return None
    bulls, bears = dii_consideration(
        IndicatorEngine(df).directional_imbalance_index(LENGTH, PERIOD))
    if action == "SELL":
        value, label = bears, "bearish"
        lo, hi = SHORT_MIN_BEARS, SHORT_MAX_BEARS
    else:
        value, label = bulls, "bullish"
        lo, hi = LONG_MIN_BULLS, LONG_MAX_BULLS
    if not np.isfinite(value):
        return f"DII {label} percentage unavailable ({LENGTH}, {PERIOD})."
    if not lo <= value <= hi:
        return (f"DII {label} percentage {value:.2f}% outside required "
                f"{lo:.0f}%–{hi:.0f}% for {action} ({LENGTH}, {PERIOD}).")
    return None
