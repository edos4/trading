"""LuxAlgo SuperTrend AI (clustering).

Reel settings: ATR length 13, factor range 1 to 5 step 0.5, performance
memory 10, from cluster Best, max iterations 1782, historical bars 65000.
Each factor's trailing performance is clustered; the factor nearest the
best centroid drives a normal supertrend flip.
"""

from __future__ import annotations

import numpy as np

from indicators._math import flip_action, kmeans3, supertrend
from indicators._signal import BaseIndicator, emit


class SupertrendAiIndicator(BaseIndicator):
    MIN_BARS = 80
    ATR_PERIOD = 13
    FACTOR_MIN = 1.0
    FACTOR_MAX = 5.0
    FACTOR_STEP = 0.5
    PERF_MEMORY = 10
    MAX_ITER = 1782
    HISTORICAL_BARS = 65_000
    trails = True

    @property
    def name(self) -> str:
        return "indicator_supertrend_ai"

    def _chosen(self, df):
        frame = df.iloc[-self.HISTORICAL_BARS :]
        high = frame["high"].to_numpy(dtype=float)
        low = frame["low"].to_numpy(dtype=float)
        close = frame["close"].to_numpy(dtype=float)
        factors = np.arange(self.FACTOR_MIN, self.FACTOR_MAX + self.FACTOR_STEP / 2, self.FACTOR_STEP)
        perfs = np.empty(len(factors))
        trends = []
        for i, factor in enumerate(factors):
            st = supertrend(high, low, close, self.ATR_PERIOD, float(factor))
            trends.append(st)
            perfs[i] = _performance(close, st.trend, self.PERF_MEMORY)
        if not np.isfinite(perfs).all():
            return None
        chosen_i = _best_index(perfs, self.MAX_ITER)
        return frame, float(factors[chosen_i]), trends[chosen_i]

    def trail(self, df):
        chosen = self._chosen(df)
        if chosen is None:
            return None
        _frame, _factor, st = chosen
        if not np.isfinite(st.line[-1]) or st.line[-1] <= 0 or not np.isfinite(st.trend[-1]):
            return None
        action = "BUY" if st.trend[-1] > 0 else "SELL"
        return action, float(st.line[-1])

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        chosen = self._chosen(df)
        if chosen is None:
            return None
        frame, factor, st = chosen
        action = flip_action(st.trend)
        if action is None:
            return None
        return emit(
            self, frame, snapshot, action=action, stop=float(st.line[-1]), line=st.line,
            notes=(
                f"SuperTrend AI atr={self.ATR_PERIOD} factor={factor:g} "
                f"cluster=best {action}"
            ),
        )


def _performance(close: np.ndarray, trend: np.ndarray, memory: int) -> float:
    pct = np.zeros(len(close))
    pct[1:] = (close[1:] - close[:-1]) / np.maximum(close[:-1], 1e-9)
    prev = np.roll(trend, 1)
    prev[0] = 0.0
    signed = prev * pct
    # span EMA, same smoothing family as the performance-memory input.
    alpha = 2.0 / (memory + 1.0)
    acc = 0.0
    for value in signed:
        acc = alpha * value + (1.0 - alpha) * acc
    return float(acc)


def _best_index(perfs: np.ndarray, max_iter: int) -> int:
    labels, centroids = kmeans3(perfs, max_iter=max_iter)
    best = int(np.nanargmax(centroids))
    members = np.flatnonzero(labels == best)
    if len(members) == 0:
        return int(np.argmax(perfs))
    return int(members[int(np.argmin(np.abs(perfs[members] - centroids[best])))])
