"""Gaussian Channel [DW].

The reel selects Donovan Wall's Gaussian Channel and leaves the defaults:
4 poles, sampling period 144, reduced lag on, fast response off,
true-range multiplier 1.414. A signal is the filter slope flipping.
"""

from __future__ import annotations

import numpy as np

from indicators._math import gaussian_channel, slope_flip
from indicators._signal import BaseIndicator, emit


class GaussianChannelIndicator(BaseIndicator):
    MIN_BARS = 160
    PERIOD = 144
    POLES = 4
    MULTIPLIER = 1.414
    trails = True

    @property
    def name(self) -> str:
        return "indicator_gaussian_channel"

    def _channel(self, df):
        return gaussian_channel(
            df["high"], df["low"], df["close"],
            period=self.PERIOD, poles=self.POLES, multiplier=self.MULTIPLIER,
        )

    def trail(self, df):
        channel = self._channel(df)
        filt = channel.filt
        if len(filt) < 2 or not np.isfinite(filt[-1]) or not np.isfinite(filt[-2]):
            return None
        if filt[-1] > filt[-2]:
            stop = float(channel.lower[-1])
            if np.isfinite(stop) and stop > 0:
                return "BUY", stop
            return None
        if filt[-1] < filt[-2]:
            stop = float(channel.upper[-1])
            if np.isfinite(stop) and stop > 0:
                return "SELL", stop
        return None

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        channel = self._channel(df)
        action = slope_flip(channel.filt)
        if action is None:
            return None
        band = float(channel.lower[-1] if action == "BUY" else channel.upper[-1])
        return emit(
            self, df, snapshot, action=action, stop=band, line=channel.filt,
            notes=f"Gaussian Channel poles={self.POLES} period={self.PERIOD} slope {action}",
        )
