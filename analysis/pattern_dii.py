"""Mandatory DII context gate for pattern scanning (defaults 10, 70)."""
from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine


def describe_pattern_dii_rejection(df) -> str | None:
    """Accept exactly 45 <= bulls <= 55; unavailable readings fail closed."""
    if df is None or len(df) < 80:
        return "DII bullish percentage unavailable: need 80 candles (10, 70)."
    bulls = IndicatorEngine(df).directional_imbalance_index()["bulls"].iloc[-1]
    if not np.isfinite(bulls):
        return "DII bullish percentage unavailable (10, 70)."
    if not 45 <= bulls <= 55:
        return f"DII bullish percentage {bulls:.2f}% outside required 45%–55% (10, 70)."
    return None
