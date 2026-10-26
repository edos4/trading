"""Directional Imbalance Index [BigBeluga] as a standalone local indicator.

Calculation attribution/license live in IndicatorEngine.directional_imbalance_index.
Bot policy added here: enter on a dominance transition, trail the opposite
price band, and exit on balanced/unavailable context or lost dominance.
The original Pine script specifies no entry or exit policy.
"""
from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from indicators._signal import BaseIndicator, emit


class DirectionalImbalanceIndexIndicator(BaseIndicator):
    LENGTH = 20
    PERIOD = 40
    MIN_BARS = LENGTH + PERIOD + 2
    trails = True

    @property
    def name(self) -> str:
        return "indicator_directional_imbalance_index"

    def _state(self, df):
        return IndicatorEngine(df).directional_imbalance_index(self.LENGTH, self.PERIOD)

    def trail(self, df):
        if df is None or len(df) < self.MIN_BARS:
            return None
        state = self._state(df)
        bulls = state["bulls"].iloc[-1]
        if not np.isfinite(bulls) or bulls == 50:
            return None
        action = "BUY" if bulls > 50 else "SELL"
        stop = state["lower" if action == "BUY" else "upper"].iloc[-1]
        price = float(df["close"].iloc[-1])
        if not np.isfinite(stop) or not np.isfinite(price) or stop <= 0 or price <= 0:
            return None
        if (action == "BUY" and stop < price) or (action == "SELL" and stop > price):
            return action, float(stop)
        return None

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or len(df) < self.MIN_BARS:
            return None
        state = self._state(df)
        previous, current = state["bulls"].iloc[-2:]
        if not np.isfinite(previous) or not np.isfinite(current):
            return None
        if previous <= 50 < current:
            action, band = "BUY", "lower"
        elif previous >= 50 > current:
            action, band = "SELL", "upper"
        else:
            return None
        return emit(
            self, df, snapshot, action=action, stop=float(state[band].iloc[-1]),
            notes=(f"DII ({self.LENGTH}, {self.PERIOD}) dominance {action}: "
                   f"bulls {current:.2f}%, bears {state['bears'].iloc[-1]:.2f}%"),
        )
