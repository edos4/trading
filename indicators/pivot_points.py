"""Pivot Points Standard (traditional / floor).

The reel opens the built-in pivot-point levels (P, R1, S1). On a daily
chart TradingView's auto timeframe is the prior month; on a weekly chart
it is the prior year. A signal is a close crossing that pivot, with the
stop on S1 (long) or R1 (short) and the target on the opposite level.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators._math import floor_pivots
from indicators._signal import BaseIndicator, emit


class PivotPointsIndicator(BaseIndicator):
    MIN_BARS = 40

    @property
    def name(self) -> str:
        return "indicator_pivot_points"

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or not isinstance(df.index, pd.DatetimeIndex):
            return None
        if len(df) < 2:
            return None
        now = _pivots_for(df, len(df) - 1, snapshot.timeframe)
        prev = _pivots_for(df, len(df) - 2, snapshot.timeframe)
        if now is None or prev is None or abs(now.p - prev.p) > 1e-8:
            return None
        prev_close = float(df["close"].iloc[-2])
        price = float(df["close"].iloc[-1])
        if prev_close <= now.p < price:
            action, stop, target = "BUY", now.s1, now.r1
        elif prev_close >= now.p > price:
            action, stop, target = "SELL", now.r1, now.s1
        else:
            return None
        return emit(
            self, df, snapshot, action=action, stop=stop, target=target,
            notes=f"Pivot P={now.p:.4f} R1={now.r1:.4f} S1={now.s1:.4f} {action}",
        )


def _pivots_for(df: pd.DataFrame, index: int, timeframe: str):
    ts = df.index[index]
    if timeframe == "1W":
        mask = df.index.year == (ts.year - 1)
    else:
        year = ts.year - 1 if ts.month == 1 else ts.year
        month = 12 if ts.month == 1 else ts.month - 1
        mask = (df.index.year == year) & (df.index.month == month)
    mask = np.asarray(mask) & (np.arange(len(df)) <= index)
    if not mask.any():
        return None
    window = df.iloc[np.flatnonzero(mask)]
    return floor_pivots(
        float(window["high"].max()),
        float(window["low"].min()),
        float(window["close"].iloc[-1]),
    )
