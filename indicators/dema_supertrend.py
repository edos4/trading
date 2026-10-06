"""DEMA and SuperTrend, from Trading Lab — 130% in 100 trades.

Applied on the chart before the settings dialog closed: DEMA 200 on close,
Supertrend 10 / hl2 / 3. Long when the supertrend is bullish and close is
above the DEMA, short on the mirror, and only on the bar that combination
turns true.
"""

from __future__ import annotations

import numpy as np

from indicators._math import dema, supertrend
from indicators._signal import BaseIndicator, emit


class DemaSupertrendIndicator(BaseIndicator):
    MIN_BARS = 220
    DEMA_LENGTH = 200
    ATR_PERIOD = 10
    MULTIPLIER = 3.0
    trails = True

    @property
    def name(self) -> str:
        return "indicator_dema_supertrend"

    def _combo(self, df):
        close = df["close"].to_numpy(dtype=float)
        st = supertrend(df["high"], df["low"], df["close"], self.ATR_PERIOD, self.MULTIPLIER)
        dema_line = dema(close, self.DEMA_LENGTH)
        return close, st, dema_line

    def trail(self, df):
        close, st, dema_line = self._combo(df)
        if not np.isfinite(dema_line[-1]) or not np.isfinite(st.line[-1]) or st.line[-1] <= 0:
            return None
        if st.trend[-1] > 0 and close[-1] > dema_line[-1]:
            return "BUY", float(st.line[-1])
        if st.trend[-1] < 0 and close[-1] < dema_line[-1]:
            return "SELL", float(st.line[-1])
        return None

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        close, st, dema_line = self._combo(df)
        if not np.isfinite(dema_line[-1]) or not np.isfinite(dema_line[-2]):
            return None
        bull = (st.trend > 0) & (close > dema_line)
        bear = (st.trend < 0) & (close < dema_line)
        if bull[-1] and not bull[-2]:
            action = "BUY"
        elif bear[-1] and not bear[-2]:
            action = "SELL"
        else:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(st.line[-1]), line=st.line,
            notes=(
                f"DEMA {self.DEMA_LENGTH} + Supertrend {self.ATR_PERIOD} "
                f"hl2 x{self.MULTIPLIER:g} {action}"
            ),
        )
