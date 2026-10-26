"""Mandatory DII context gate for pattern scanning (defaults 20, 40)."""
from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine

LENGTH = 20
PERIOD = 40
MIN_BARS = LENGTH + PERIOD


def describe_pattern_dii_rejection(df) -> str | None:
    """Accept exactly 45 <= bulls <= 55; unavailable readings fail closed."""
    if df is None or len(df) < MIN_BARS:
        return f"DII bullish percentage unavailable: need {MIN_BARS} candles ({LENGTH}, {PERIOD})."
    bulls = IndicatorEngine(df).directional_imbalance_index(LENGTH, PERIOD)["bulls"].iloc[-1]
    if not np.isfinite(bulls):
        return f"DII bullish percentage unavailable ({LENGTH}, {PERIOD})."
    if not 45 <= bulls <= 55:
        return f"DII bullish percentage {bulls:.2f}% outside required 45%–55% ({LENGTH}, {PERIOD})."
    return None
