"""Supertrend from TRADING CHART — Powerful profitable strategy indicator.

KivancOzbilgic settings left on the chart: ATR period 14, multiplier 3,
source hl2, "Change ATR Calculation Method" checked (Wilder ATR). Buy/sell
on the trend flip.
"""

from __future__ import annotations

import numpy as np

from indicators._math import flip_action, supertrend
from indicators._signal import BaseIndicator, emit


class SupertrendIndicator(BaseIndicator):
    MIN_BARS = 30
    ATR_PERIOD = 14
    MULTIPLIER = 3.0
    trails = True

    @property
    def name(self) -> str:
        return "indicator_supertrend"

    def _state(self, df):
        return supertrend(
            df["high"], df["low"], df["close"], self.ATR_PERIOD, self.MULTIPLIER,
        )

    def trail(self, df):
        st = self._state(df)
        if not np.isfinite(st.line[-1]) or st.line[-1] <= 0 or not np.isfinite(st.trend[-1]):
            return None
        action = "BUY" if st.trend[-1] > 0 else "SELL"
        return action, float(st.line[-1])

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        st = self._state(df)
        action = flip_action(st.trend)
        if action is None:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(st.line[-1]), line=st.line,
            notes=f"Supertrend {self.ATR_PERIOD} hl2 x{self.MULTIPLIER:g} Wilder flip {action}",
        )
