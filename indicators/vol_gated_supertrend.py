"""Volatility Gated Supertrend [BackQuant].

Reel settings: ATR multiplier 4, ATR length 17, volatility gate on,
fast ATR 7, slow ATR 50, gate threshold 0.75. A flip prints only when
fast ATR / slow ATR is at least the threshold (the gray "gated" state is
a compression, and those flips are dropped).
"""

from __future__ import annotations

import numpy as np

from indicators._math import flip_action, supertrend, wilder_atr
from indicators._signal import BaseIndicator, emit


class VolGatedSupertrendIndicator(BaseIndicator):
    MIN_BARS = 60
    ATR_PERIOD = 17
    MULTIPLIER = 4.0
    FAST_ATR = 7
    SLOW_ATR = 50
    GATE_THRESHOLD = 0.75
    trails = True

    @property
    def name(self) -> str:
        return "indicator_vol_gated_supertrend"

    def _supertrend(self, df):
        return supertrend(
            df["high"], df["low"], df["close"], self.ATR_PERIOD, self.MULTIPLIER,
        )

    def trail(self, df):
        # The gate filters entries. An open trade follows the supertrend line.
        st = self._supertrend(df)
        if not np.isfinite(st.line[-1]) or st.line[-1] <= 0 or not np.isfinite(st.trend[-1]):
            return None
        action = "BUY" if st.trend[-1] > 0 else "SELL"
        return action, float(st.line[-1])

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        high, low, close = df["high"], df["low"], df["close"]
        st = self._supertrend(df)
        action = flip_action(st.trend)
        if action is None:
            return None
        fast = wilder_atr(high, low, close, self.FAST_ATR)
        slow = wilder_atr(high, low, close, self.SLOW_ATR)
        if not np.isfinite(slow[-1]) or slow[-1] <= 0 or not np.isfinite(fast[-1]):
            return None
        ratio = float(fast[-1] / slow[-1])
        if ratio < self.GATE_THRESHOLD:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(st.line[-1]), line=st.line,
            notes=(
                f"Vol-gated ST atr={self.ATR_PERIOD} x{self.MULTIPLIER:g} "
                f"gate {ratio:.2f}>={self.GATE_THRESHOLD:g} {action}"
            ),
        )
