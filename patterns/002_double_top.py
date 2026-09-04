from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rules import extrema, weak_leg_volume
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_TARGET,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
)


@dataclass(frozen=True)
class _Setup:
    h1: int
    h2: int
    valley: int
    neckline: float
    h1_high: float
    h2_high: float
    h1_rsi: float
    h2_rsi: float
    entry: int


class DoubleTopPattern(BasePattern):
    RSI_PERIOD = 14
    H1_RSI_MIN = 70.0
    H2_RSI_MIN = 50.0
    H2_RSI_MAX = 61.0
    RSI_DIVERGENCE_MIN = 3.0
    VALLEY_DEPTH_MIN = 0.05
    GAP_MIN = 8
    GAP_MAX = 90
    ENTRY_DELAY = 7
    TARGET_BELOW_NECKLINE = 0.07
    EXIT_AFTER_NECKLINE_BREAK = 5
    TRAILING_STOP_PCT = 0.03
    SWING_LOOKBACK = 2
    MIN_BARS = 110
    SHARES = 25

    @property
    def name(self) -> str:
        return "pattern_002_double_top"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Double top with a lower second high, bearish RSI divergence, a five-percent valley, weak recovery volume, and a day-seven-or-neckline-break short entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi(self.RSI_PERIOD)
        current = len(df) - 1
        highs = extrema(ind.high, "high", self.SWING_LOOKBACK)
        for h2 in reversed(highs):
            if h2 + 2 > current:
                continue
            for h1 in reversed([idx for idx in highs if idx < h2]):
                setup = self._evaluate(ind, rsi, h1, h2, current)
                if setup is None or setup.entry != current:
                    continue
                price = float(ind.close.iloc[current])
                target = round(setup.neckline * (1 - self.TARGET_BELOW_NECKLINE), 4)
                return TradeSignal(
                    symbol=snapshot.symbol,
                    action="SELL",
                    pattern=self.name,
                    timeframe=snapshot.timeframe,
                    confidence=1.0,
                    price=price,
                    qty=self.SHARES,
                    take_profit=target,
                    trailing_stop_pct=self.TRAILING_STOP_PCT,
                    trailing_stop_mode="lowest_close",
                    trailing_activation_pct=0.0,
                    neckline=setup.neckline,
                    neckline_break_direction="below",
                    exit_bars_after_neckline_break=self.EXIT_AFTER_NECKLINE_BREAK,
                    notes=f"Double top H1={h1} H2={h2} neckline={setup.neckline:.2f} RSI={setup.h1_rsi:.1f}->{setup.h2_rsi:.1f}",
                    chart_annotations=[
                        ann_marker(self.bar_date(df, h1), setup.h1_high, "H1", ANN_PEAK, "v", "above"),
                        ann_marker(self.bar_date(df, setup.valley), setup.neckline, "neckline", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, h2), setup.h2_high, "H2", ANN_PEAK, "v", "above"),
                        ann_hline(setup.neckline, "neckline", ANN_LINE),
                        ann_hline(target, "target", ANN_TARGET),
                        ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "above"),
                    ],
                )
        return None

    def _evaluate(self, ind: IndicatorEngine, rsi, h1: int, h2: int, current: int) -> _Setup | None:
        gap = h2 - h1
        if gap < self.GAP_MIN or gap > self.GAP_MAX:
            return None
        h1_high = float(ind.high.iloc[h1])
        h2_high = float(ind.high.iloc[h2])
        h1_close = float(ind.close.iloc[h1])
        h2_close = float(ind.close.iloc[h2])
        h1_rsi = float(rsi.iloc[h1])
        h2_rsi = float(rsi.iloc[h2])
        if not np.isfinite([h1_rsi, h2_rsi]).all():
            return None
        if h2_high >= h1_high or h2_close >= h1_close:
            return None
        if h1_rsi < self.H1_RSI_MIN or h2_rsi < self.H2_RSI_MIN or h2_rsi > self.H2_RSI_MAX:
            return None
        if h1_rsi - h2_rsi <= self.RSI_DIVERGENCE_MIN:
            return None
        between_highs = ind.high.iloc[h1 + 1 : h2]
        if not between_highs.empty and float(between_highs.max()) > h1_high:
            return None
        valley_slice = ind.low.iloc[h1 + 1 : h2]
        if valley_slice.empty:
            return None
        valley = h1 + 1 + int(np.argmin(valley_slice.to_numpy(dtype=float)))
        neckline = float(ind.low.iloc[valley])
        if (h1_high - neckline) / h1_high < self.VALLEY_DEPTH_MIN:
            return None
        if float(ind.close.iloc[h2 + 1]) >= h2_close or float(ind.close.iloc[h2 + 2]) >= h2_close:
            return None
        if not weak_leg_volume(ind.open, ind.close, ind.volume, valley, h2, "up"):
            return None
        break_index = next(
            (idx for idx in range(h2 + 1, current + 1) if float(ind.close.iloc[idx]) < neckline),
            None,
        )
        entry = min(h2 + self.ENTRY_DELAY, break_index) if break_index is not None else h2 + self.ENTRY_DELAY
        if entry > current:
            return None
        invalidation_stop = break_index if break_index is not None else current + 1
        post_h2 = ind.high.iloc[h2 + 1 : invalidation_stop]
        if not post_h2.empty and float(post_h2.max()) > h2_high:
            return None
        return _Setup(h1, h2, valley, neckline, h1_high, h2_high, h1_rsi, h2_rsi, entry)
