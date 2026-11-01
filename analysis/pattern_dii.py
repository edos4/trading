"""Mandatory DII context gate for pattern scanning (long 16–40, short 40–60)."""

from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine

LENGTH = 20
PERIOD = 40
MIN_BARS = LENGTH + PERIOD
LONG_MIN_BULLS = 16.0
LONG_MAX_BULLS = 40.0
SHORT_MIN_BULLS = 40.0
SHORT_MAX_BULLS = 60.0
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

    BUY (long) accepts 16 <= bulls <= 40; SELL (short) accepts 40 <= bulls <= 60.
    A short warmup window (e.g. the first PATTERN_SCAN_HISTORY_BARS bars after
    a process start, while bulk history is still preloading) carries no
    rejection — the gate engages once enough candles are stored. A zero-event
    window is balanced, not unavailable.
    """
    if df is None or len(df) < MIN_BARS:
        return None
    bulls, _bears = dii_consideration(
        IndicatorEngine(df).directional_imbalance_index(LENGTH, PERIOD))
    if not np.isfinite(bulls):
        return f"DII bullish percentage unavailable ({LENGTH}, {PERIOD})."
    lo, hi = (LONG_MIN_BULLS, LONG_MAX_BULLS) if action == "BUY" else (SHORT_MIN_BULLS, SHORT_MAX_BULLS)
    if not lo <= bulls <= hi:
        return (f"DII bullish percentage {bulls:.2f}% outside required "
                f"{lo:.0f}%–{hi:.0f}% for {action} ({LENGTH}, {PERIOD}).")
    return None
