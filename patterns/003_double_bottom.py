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
    l1: int
    l2: int
    peak: int
    neckline: float
    l1_low: float
    l2_low: float
    l1_rsi: float
    l2_rsi: float
    entry: int


class DoubleBottomPattern(BasePattern):
    RSI_PERIOD = 14
    L1_RSI_MAX = 30.0
    L2_RSI_MIN = 39.0
    L2_RSI_MAX = 50.0
    RSI_DIVERGENCE_MIN = 3.0
    PEAK_HEIGHT_MIN = 0.05
    GAP_MIN = 8
    GAP_MAX = 90
    ENTRY_DELAY = 7
    TARGET_ABOVE_NECKLINE = 0.07
    EXIT_AFTER_NECKLINE_BREAK = 5
    TRAILING_STOP_PCT = 0.03
    SWING_LOOKBACK = 2
    MIN_BARS = 110
    SHARES = 25

    @property
    def name(self) -> str:
        return "pattern_003_double_bottom"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Double bottom with a higher second low, bullish RSI divergence, a five-percent intervening peak, weak selloff volume, and a day-seven-or-neckline-break long entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi_wilder(self.RSI_PERIOD)
        current = len(df) - 1
        lows = extrema(ind.low, "low", self.SWING_LOOKBACK)
        for l2 in reversed(lows):
            if l2 + 2 > current:
                continue
            for l1 in reversed([idx for idx in lows if idx < l2]):
                setup = self._evaluate(ind, rsi, l1, l2, current)
                if setup is None or setup.entry != current:
                    continue
                price = float(ind.close.iloc[current])
                target = round(setup.neckline * (1 + self.TARGET_ABOVE_NECKLINE), 4)
                return TradeSignal(
                    symbol=snapshot.symbol,
                    action="BUY",
                    pattern=self.name,
                    timeframe=snapshot.timeframe,
                    confidence=1.0,
                    price=price,
                    qty=self.SHARES,
                    take_profit=target,
                    trailing_stop_pct=self.TRAILING_STOP_PCT,
                    trailing_stop_mode="highest_close",
                    trailing_activation_pct=0.0,
                    neckline=setup.neckline,
                    neckline_break_direction="above",
                    exit_bars_after_neckline_break=self.EXIT_AFTER_NECKLINE_BREAK,
                    notes=f"Double bottom L1={l1} L2={l2} neckline={setup.neckline:.2f} RSI={setup.l1_rsi:.1f}->{setup.l2_rsi:.1f}",
                    chart_annotations=[
                        ann_marker(self.bar_date(df, l1), setup.l1_low, "L1", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, setup.peak), setup.neckline, "neckline", ANN_PEAK, "v", "above"),
                        ann_marker(self.bar_date(df, l2), setup.l2_low, "L2", ANN_TROUGH, "^", "below"),
                        ann_hline(setup.neckline, "neckline", ANN_LINE),
                        ann_hline(target, "target", ANN_TARGET),
                        ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "below"),
                    ],
                )
        return None

    def _evaluate(self, ind: IndicatorEngine, rsi, l1: int, l2: int, current: int) -> _Setup | None:
        gap = l2 - l1
        if gap < self.GAP_MIN or gap > self.GAP_MAX:
            return None
        l1_low = float(ind.low.iloc[l1])
        l2_low = float(ind.low.iloc[l2])
        l1_close = float(ind.close.iloc[l1])
        l2_close = float(ind.close.iloc[l2])
        l1_rsi = float(rsi.iloc[l1])
        l2_rsi = float(rsi.iloc[l2])
        if not np.isfinite([l1_rsi, l2_rsi]).all():
            return None
        if l2_low <= l1_low or l2_close <= l1_close:
            return None
        if l1_rsi > self.L1_RSI_MAX or l2_rsi < self.L2_RSI_MIN or l2_rsi > self.L2_RSI_MAX:
            return None
        if l2_rsi - l1_rsi <= self.RSI_DIVERGENCE_MIN:
            return None
        between_lows = ind.low.iloc[l1 + 1 : l2]
        if not between_lows.empty and float(between_lows.min()) < l1_low:
            return None
        peak_slice = ind.high.iloc[l1 + 1 : l2]
        if peak_slice.empty:
            return None
        peak = l1 + 1 + int(np.argmax(peak_slice.to_numpy(dtype=float)))
        neckline = float(ind.high.iloc[peak])
        if (neckline - l1_low) / l1_low < self.PEAK_HEIGHT_MIN:
            return None
        if float(ind.close.iloc[l2 + 1]) <= l2_close or float(ind.close.iloc[l2 + 2]) <= l2_close:
            return None
        if not weak_leg_volume(ind.open, ind.close, ind.volume, peak, l2, "down"):
            return None
        break_index = next(
            (idx for idx in range(l2 + 1, current + 1) if float(ind.close.iloc[idx]) > neckline),
            None,
        )
        entry = min(l2 + self.ENTRY_DELAY, break_index) if break_index is not None else l2 + self.ENTRY_DELAY
        if entry > current:
            return None
        invalidation_stop = break_index if break_index is not None else current + 1
        post_l2 = ind.low.iloc[l2 + 1 : invalidation_stop]
        if not post_l2.empty and float(post_l2.min()) < l2_low:
            return None
        return _Setup(l1, l2, peak, neckline, l1_low, l2_low, l1_rsi, l2_rsi, entry)
