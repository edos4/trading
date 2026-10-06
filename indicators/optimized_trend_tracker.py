"""Optimized Trend Tracker (OTT).

Reel settings: source close, period 7, percent 1.4, average type VAR,
support-line crossing signals on, price/OTT crossing off.
"""

from __future__ import annotations

import numpy as np

from indicators._math import optimized_trend_tracker, support_line_cross
from indicators._signal import BaseIndicator, emit


class OttIndicator(BaseIndicator):
    MIN_BARS = 40
    PERIOD = 7
    PERCENT = 1.4
    trails = True

    @property
    def name(self) -> str:
        return "indicator_ott"

    def _result(self, df):
        close = df["close"].to_numpy(dtype=float)
        return close, optimized_trend_tracker(close, self.PERIOD, self.PERCENT)

    def trail(self, df):
        close, result = self._result(df)
        stop = float(result.support[-1])
        price = float(close[-1])
        if not np.isfinite(stop) or stop <= 0 or not np.isfinite(price):
            return None
        if stop < price:
            return "BUY", stop
        if stop > price:
            return "SELL", stop
        return None

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        _close, result = self._result(df)
        action = support_line_cross(result)
        if action is None:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(result.support[-1]), line=result.ott,
            notes=f"OTT VAR period={self.PERIOD} percent={self.PERCENT:g} support cross {action}",
        )
