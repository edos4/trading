from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns._rules import extrema, notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_REF,
    ANN_STOP,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
    ann_segment,
)


@dataclass(frozen=True)
class _Setup:
    pole_start: int
    pole_end: int
    flag_start: int
    flag_end: int
    pole_high: float
    flag_high: float
    flag_low: float
    pole_gain: float
    pole_volume_ratio: float
    flag_depth: float
    flag_drift: float
    flag_volume_ratio: float


class FlagPattern(BasePattern):
    MIN_BARS = 120
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_009_flag_pattern"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Bull flag with a twenty-five-percent high-volume pole, a short low-volume ten-to-thirty-four-percent pullback, and a volume-confirmed breakout."

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
        stop = round(max(setup.flag_low, price * 0.97), 4)
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=notional_qty(self.POSITION_NOTIONAL, price),
            stop_loss=stop,
            trailing_stop_pct=0.03,
            trailing_stop_mode="highest_close",
            trailing_activation_pct=0.0,
            notes=f"Bull flag pole={setup.pole_start}->{setup.pole_end} gain={setup.pole_gain:.1%} volume={setup.pole_volume_ratio:.2f}x flag={setup.flag_start}->{setup.flag_end} depth={setup.flag_depth:.1%} volume={setup.flag_volume_ratio:.2f}x",
            chart_annotations=[
                ann_marker(self.bar_date(df, setup.pole_start), float(ind.open.iloc[setup.pole_start]), "pole start", ANN_REF, "^", "below"),
                ann_marker(self.bar_date(df, setup.pole_end), setup.pole_high, "pole high", ANN_PEAK, "v", "above"),
                ann_segment(self.bar_date(df, setup.pole_start), self.bar_date(df, setup.pole_end), float(ind.open.iloc[setup.pole_start]), setup.pole_high, ANN_LINE),
                ann_marker(self.bar_date(df, setup.flag_end), setup.flag_low, "flag low", ANN_TROUGH, "^", "below"),
                ann_hline(setup.flag_high, "flag high", ANN_LINE),
                ann_hline(stop, "stop", ANN_STOP),
                ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "below"),
            ],
        )

    def _find_setup(self, ind: IndicatorEngine, current: int) -> _Setup | None:
        sma = ind.sma(50)
        for pole_end in reversed(extrema(ind.high, "high", 2)):
            distance = current - pole_end
            if distance < 5 or distance > 35:
                continue
            pole = self._find_pole(ind, sma, pole_end)
            if pole is None:
                continue
            pole_start, pole_gain, pole_volume_ratio, pole_average = pole
            pole_high = float(ind.high.iloc[pole_end])
            flag_start = pole_end + 1
            for flag_length in range(4, 16):
                flag_end = flag_start + flag_length - 1
                if flag_end >= current or current - flag_end > 20:
                    continue
                flag_high = float(ind.high.iloc[flag_start : flag_end + 1].max())
                flag_low = float(ind.low.iloc[flag_start : flag_end + 1].min())
                depth = (pole_high - flag_low) / pole_high
                if depth < 0.10 or depth > 0.34:
                    continue
                start_close = float(ind.close.iloc[flag_start])
                drift = (float(ind.close.iloc[flag_end]) - start_close) / start_close
                if drift > 0.06:
                    continue
                flag_average = float(ind.volume.iloc[flag_start : flag_end + 1].mean())
                flag_volume_ratio = flag_average / pole_average
                if flag_volume_ratio > 0.85:
                    continue
                if any(float(ind.close.iloc[idx]) > flag_high for idx in range(flag_end + 1, current)):
                    continue
                if float(ind.close.iloc[current]) <= flag_high or float(ind.volume.iloc[current]) < flag_average:
                    continue
                return _Setup(
                    pole_start,
                    pole_end,
                    flag_start,
                    flag_end,
                    pole_high,
                    flag_high,
                    flag_low,
                    pole_gain,
                    pole_volume_ratio,
                    depth,
                    drift,
                    flag_volume_ratio,
                )
        return None

    def _find_pole(self, ind: IndicatorEngine, sma: pd.Series, pole_end: int) -> tuple[int, float, float, float] | None:
        best: tuple[int, float, float, float] | None = None
        for length in range(3, 41):
            start = pole_end - length + 1
            baseline_start = start - 20
            if baseline_start < 0:
                continue
            start_open = float(ind.open.iloc[start])
            gain = (float(ind.close.iloc[pole_end]) - start_open) / start_open
            if gain < 0.25:
                continue
            baseline = float(ind.volume.iloc[baseline_start:start].mean())
            pole_average = float(ind.volume.iloc[start : pole_end + 1].mean())
            if baseline <= 0 or pole_average < baseline * 1.15:
                continue
            previous_sma = start - 5
            if previous_sma < 0 or pd.isna(sma.iloc[start]) or pd.isna(sma.iloc[previous_sma]):
                continue
            if float(ind.close.iloc[start]) < float(sma.iloc[start]) or float(sma.iloc[start]) <= float(sma.iloc[previous_sma]):
                continue
            candidate = (start, gain, pole_average / baseline, pole_average)
            if best is None or gain > best[1]:
                best = candidate
        return best
