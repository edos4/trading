"""Mandatory DII context gate for pattern scanning (BUY bulls 17–40, SELL bears 21–42)."""

from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine

LENGTH = 20
PERIOD = 40
MIN_BARS = LENGTH + PERIOD
LONG_MIN_BULLS = 17.0
LONG_MAX_BULLS = 40.0
SHORT_MIN_BEARS = 21.0
SHORT_MAX_BEARS = 42.0
BALANCED = 50.0


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

    BUY (long) accepts 17 <= bulls <= 40; SELL (short) accepts 21 <= bears <= 42.
    Because bulls + bears == 100, those bands do not overlap: a tape with bulls
    17–40 passes only BUY, a tape with bulls 58–79 (bears 21–42) passes only
    SELL, and the 40–58 middle (e.g. a balanced 50/50 tape) passes neither.
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
