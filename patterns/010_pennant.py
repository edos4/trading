from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rules import notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_REF,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_marker,
    ann_segment,
)


@dataclass(frozen=True)
class _Setup:
    direction: Literal["bull", "bear"]
    pole_start: int
    pole_end: int
    consolidation_start: int
    consolidation_end: int
    pole_move: float
    pole_volume_ratio: float
    retrace: float
    consolidation_volume_ratio: float
    breakout_volume_ratio: float
    upper_at_breakout: float
    lower_at_breakout: float


class PennantPattern(BasePattern):
    MIN_BARS = 50
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_010_pennant"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Bullish or bearish continuation pennant with a ten-percent high-volume impulse, a five-to-ten-day converging low-volume coil, and a volume-confirmed continuation breakout."

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
        bullish = setup.direction == "bull"
        action: Literal["BUY", "SELL"] = "BUY" if bullish else "SELL"
        start_price = float(ind.low.iloc[setup.pole_start] if bullish else ind.high.iloc[setup.pole_start])
        extreme_price = float(ind.high.iloc[setup.pole_end] if bullish else ind.low.iloc[setup.pole_end])
        breakout_line = setup.upper_at_breakout if bullish else setup.lower_at_breakout
        return TradeSignal(
            symbol=snapshot.symbol,
            action=action,
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=notional_qty(self.POSITION_NOTIONAL, price),
            trailing_stop_pct=0.05,
            trailing_stop_mode="highest_close" if bullish else "lowest_close",
            trailing_activation_pct=0.0,
            trailing_stop_on_close=True,
            exit_bars_after_entry=60,   # .cjs backtest_pennant_200: MAX_HOLD_BARS
            notes=f"{setup.direction} pennant pole={setup.pole_start}->{setup.pole_end} move={setup.pole_move:.1%} pole_volume={setup.pole_volume_ratio:.2f}x consolidation={setup.consolidation_start}->{setup.consolidation_end} retrace={setup.retrace:.1%} breakout_volume={setup.breakout_volume_ratio:.2f}x",
            chart_annotations=[
                ann_marker(self.bar_date(df, setup.pole_start), start_price, "pole start", ANN_REF, "o", "below" if bullish else "above"),
                ann_marker(self.bar_date(df, setup.pole_end), extreme_price, "pole", ANN_PEAK if bullish else ANN_TROUGH, "v" if bullish else "^", "above" if bullish else "below"),
                ann_segment(self.bar_date(df, setup.pole_start), self.bar_date(df, setup.pole_end), start_price, extreme_price, ANN_LINE),
                ann_marker(self.bar_date(df, current), breakout_line, "breakout line", ANN_LINE, "o", "above" if bullish else "below"),
                ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "below" if bullish else "above"),
            ],
        )

    def _find_setup(self, ind: IndicatorEngine, current: int) -> _Setup | None:
        consolidation_end = current - 1
        for direction in ("bull", "bear"):
            for length in range(5, 11):
                consolidation_start = consolidation_end - length + 1
                for offset in (1, 2):
                    pole_end = consolidation_start - offset
                    pole = self._find_pole(ind, pole_end, direction)
                    if pole is None:
                        continue
                    pole_start, move, pole_range, pole_volume_ratio, pole_average = pole
                    setup = self._check_consolidation(
                        ind,
                        current,
                        direction,
                        pole_start,
                        pole_end,
                        consolidation_start,
                        consolidation_end,
                        move,
                        pole_range,
                        pole_volume_ratio,
                        pole_average,
                    )
                    if setup is not None:
                        return setup
        return None

    def _find_pole(self, ind: IndicatorEngine, pole_end: int, direction: str) -> tuple[int, float, float, float, float] | None:
        best: tuple[int, float, float, float, float] | None = None
        for length in range(1, 11):
            start = pole_end - length + 1
            baseline_start = start - 20
            if baseline_start < 0:
                continue
            if direction == "bull":
                start_price = float(ind.low.iloc[start])
                extreme = float(ind.high.iloc[pole_end])
                if extreme != float(ind.high.iloc[start : pole_end + 1].max()):
                    continue
                pole_range = extreme - start_price
            else:
                start_price = float(ind.high.iloc[start])
                extreme = float(ind.low.iloc[pole_end])
                if extreme != float(ind.low.iloc[start : pole_end + 1].min()):
                    continue
                pole_range = start_price - extreme
            if start_price <= 0 or pole_range / start_price < 0.10:
                continue
            baseline = float(ind.volume.iloc[baseline_start:start].mean())
            pole_average = float(ind.volume.iloc[start : pole_end + 1].mean())
            if baseline <= 0 or pole_average < baseline * 1.30:
                continue
            candidate = (start, pole_range / start_price, pole_range, pole_average / baseline, pole_average)
            if best is None or candidate[1] > best[1]:
                best = candidate
        return best

    def _check_consolidation(
        self,
        ind: IndicatorEngine,
        current: int,
        direction: str,
        pole_start: int,
        pole_end: int,
        start: int,
        end: int,
        move: float,
        pole_range: float,
        pole_volume_ratio: float,
        pole_average: float,
    ) -> _Setup | None:
        x = np.arange(end - start + 1, dtype=float)
        highs = ind.high.iloc[start : end + 1].to_numpy(dtype=float)
        lows = ind.low.iloc[start : end + 1].to_numpy(dtype=float)
        try:
            upper_slope, upper_intercept = np.polyfit(x, highs, 1)
            lower_slope, lower_intercept = np.polyfit(x, lows, 1)
        except (np.linalg.LinAlgError, ValueError):
            return None
        if upper_slope >= 0 or lower_slope <= 0:
            return None
        upper = lambda value: upper_slope * value + upper_intercept
        lower = lambda value: lower_slope * value + lower_intercept
        if upper(0) <= lower(0) or upper(x[-1]) <= lower(x[-1]):
            return None
        closes = ind.close.iloc[start : end + 1].to_numpy(dtype=float)
        if any(close > upper(idx) or close < lower(idx) for idx, close in enumerate(closes)):
            return None
        if direction == "bull":
            retrace = (float(ind.high.iloc[pole_end]) - float(ind.low.iloc[start : end + 1].min())) / pole_range
        else:
            retrace = (float(ind.high.iloc[start : end + 1].max()) - float(ind.low.iloc[pole_end])) / pole_range
        if retrace < 0 or retrace > 0.30:
            return None
        consolidation_average = float(ind.volume.iloc[start : end + 1].mean())
        if consolidation_average <= 0 or consolidation_average > pole_average * 0.70:
            return None
        breakout_x = float(len(x))
        upper_breakout = float(upper(breakout_x))
        lower_breakout = float(lower(breakout_x))
        breakout_close = float(ind.close.iloc[current])
        if direction == "bull" and breakout_close <= upper_breakout:
            return None
        if direction == "bear" and breakout_close >= lower_breakout:
            return None
        breakout_volume_ratio = float(ind.volume.iloc[current]) / consolidation_average
        if breakout_volume_ratio < 1.50:
            return None
        return _Setup(
            direction,
            pole_start,
            pole_end,
            start,
            end,
            move,
            pole_volume_ratio,
            retrace,
            consolidation_average / pole_average,
            breakout_volume_ratio,
            upper_breakout,
            lower_breakout,
        )
