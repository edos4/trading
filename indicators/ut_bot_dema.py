"""UT Bot Alerts plus DEMA, from TRADING CHART — Key strategy.

UT Bot: key value 1, ATR period 21, signals from Heikin Ashi candles.
DEMA: length 21, source close. A UT Bot flip is taken only when close is
on the DEMA side of the trade (buy above, sell below).
"""

from __future__ import annotations

import numpy as np

from indicators._math import atr_trailing_stop, dema, heikin_ashi_close, ut_bot_flip, wilder_atr
from indicators._signal import BaseIndicator, emit


class UtBotDemaIndicator(BaseIndicator):
    MIN_BARS = 50
    KEY = 1.0
    ATR_PERIOD = 21
    DEMA_LENGTH = 21
    trails = True

    @property
    def name(self) -> str:
        return "indicator_ut_bot_dema"

    def _lines(self, df):
        close = df["close"].to_numpy(dtype=float)
        src = heikin_ashi_close(df["open"], df["high"], df["low"], df["close"])
        atr = wilder_atr(df["high"], df["low"], df["close"], self.ATR_PERIOD)
        stop_line = atr_trailing_stop(src, atr, self.KEY)
        return close, src, stop_line

    def trail(self, df):
        _close, src, stop_line = self._lines(df)
        stop = float(stop_line[-1])
        if not np.isfinite(stop) or stop <= 0 or not np.isfinite(src[-1]):
            return None
        if src[-1] > stop:
            return "BUY", stop
        if src[-1] < stop:
            return "SELL", stop
        return None

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        close, src, stop_line = self._lines(df)
        action = ut_bot_flip(src, stop_line)
        if action is None:
            return None
        dema_line = dema(close, self.DEMA_LENGTH)
        if not np.isfinite(dema_line[-1]):
            return None
        if action == "BUY" and close[-1] <= dema_line[-1]:
            return None
        if action == "SELL" and close[-1] >= dema_line[-1]:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(stop_line[-1]), line=stop_line,
            notes=(
                f"UT Bot key={self.KEY:g} ATR={self.ATR_PERIOD} HA + "
                f"DEMA {self.DEMA_LENGTH} {action}"
            ),
        )
