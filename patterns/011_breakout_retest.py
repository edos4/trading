from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rules import extrema, notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_STOP,
    ANN_TARGET,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
)


@dataclass(frozen=True)
class _Setup:
    range_start: int
    breakout: int
    retest: int
    resistance: float
    support: float
    resistance_touches: int
    support_touches: int


class BreakoutRetestPattern(BasePattern):
    MIN_BARS = 105
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_011_breakout_retest"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Bullish range breakout followed by a retest that holds old resistance and a bullish confirmation close above the retest bar."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        current = len(df) - 1
        setup = self._find_setup(ind, current)
        if setup is None:
            return None
        price = float(ind.close.iloc[current])
        retest_low = float(ind.low.iloc[setup.retest])
        stop = round(retest_low * 0.99, 4)
        target = round(price + setup.resistance - setup.support, 4)
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=notional_qty(self.POSITION_NOTIONAL, price),
            stop_loss=stop,
            take_profit=target,
            trailing_stop_pct=0.03,
            trailing_stop_mode="highest_close",
            trailing_activation_pct=0.04,
            notes=f"Breakout retest range={setup.range_start}->{setup.breakout} resistance={setup.resistance:.2f} support={setup.support:.2f} retest={setup.retest} touches={setup.resistance_touches}/{setup.support_touches}",
            chart_annotations=[
                ann_hline(setup.resistance, "resistance", ANN_LINE),
                ann_hline(setup.support, "support", ANN_LINE),
                ann_marker(self.bar_date(df, setup.breakout), float(ind.close.iloc[setup.breakout]), "breakout", ANN_PEAK, "^", "above"),
                ann_marker(self.bar_date(df, setup.retest), retest_low, "retest", ANN_TROUGH, "^", "below"),
                ann_hline(stop, "stop", ANN_STOP),
                ann_hline(target, "target", ANN_TARGET),
                ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "below"),
            ],
        )

    def _find_setup(self, ind: IndicatorEngine, current: int) -> _Setup | None:
        earliest = max(10, current - 13)
        for breakout in reversed(range(earliest, current)):
            post = ind.low.iloc[breakout + 1 : current + 1]
            if post.empty:
                continue
            retest = breakout + 1 + int(np.argmin(post.to_numpy(dtype=float)))
            if retest - breakout > 8 or retest >= current or current - retest > 5:
                continue
            for length in range(10, 91):
                start = breakout - length
                if start < 0:
                    break
                setup = self._evaluate_range(ind, current, start, breakout, retest)
                if setup is not None:
                    return setup
        return None

    def _evaluate_range(self, ind: IndicatorEngine, current: int, start: int, breakout: int, retest: int) -> _Setup | None:
        highs = ind.high.iloc[start:breakout]
        lows = ind.low.iloc[start:breakout]
        resistance = float(highs.max())
        support = float(lows.min())
        if resistance <= support or (resistance - support) / resistance > 0.15:
            return None
        swing_highs = [idx for idx in extrema(ind.high, "high", 2) if start <= idx < breakout]
        swing_lows = [idx for idx in extrema(ind.low, "low", 2) if start <= idx < breakout]
        resistance_points = [idx for idx in swing_highs if float(ind.high.iloc[idx]) >= resistance * 0.985]
        support_points = [idx for idx in swing_lows if float(ind.low.iloc[idx]) <= support * 1.015]
        if len(resistance_points) < 2 or len(support_points) < 2:
            return None
        range_start = min(resistance_points + support_points)
        if breakout - range_start < 10:
            return None
        if float(ind.close.iloc[breakout]) <= resistance:
            return None
        if any(float(ind.close.iloc[idx]) > resistance for idx in range(range_start, breakout)):
            return None
        retest_low = float(ind.low.iloc[retest])
        if retest_low > resistance * 1.02:
            return None
        if float(ind.close.iloc[breakout : current + 1].min()) < resistance * 0.99:
            return None
        close = float(ind.close.iloc[current])
        if close <= float(ind.open.iloc[current]) or close <= float(ind.high.iloc[retest]) or close <= resistance:
            return None
        return _Setup(range_start, breakout, retest, resistance, support, len(resistance_points), len(support_points))
