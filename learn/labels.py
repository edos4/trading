"""
learn/labels.py — triple-barrier labeling for the swing-win classifier.

For each bar, entry is that bar's close. Looking forward up to
`horizon_days` bars: label 1 ("win") if the high reaches the target price
before the low reaches the stop price; label 0 ("loss") if the stop is hit
first, or both are touched on the same bar (only daily bars are available,
so intraday order is unknown — treated conservatively as a loss). Bars that
never resolve within the horizon are left NaN and dropped by the caller —
they're ambiguous, not losses.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier_labels(
    df: pd.DataFrame, horizon_days: int, target_pct: float, stop_pct: float,
) -> pd.Series:
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(close)

    labels = np.full(n, np.nan)
    resolved = np.zeros(n, dtype=bool)
    target_price = close * (1 + target_pct)
    stop_price = close * (1 - stop_pct)
    idx = np.arange(n)

    for d in range(1, horizon_days + 1):
        valid = idx < n - d
        fwd_high = np.roll(high, -d)
        fwd_low = np.roll(low, -d)
        touched_target = valid & ~resolved & (fwd_high >= target_price)
        touched_stop = valid & ~resolved & (fwd_low <= stop_price)
        labels[touched_target & ~touched_stop] = 1.0
        labels[touched_stop] = 0.0  # covers stop-only and same-bar-both cases
        resolved |= touched_target | touched_stop

    return pd.Series(labels, index=df.index)
