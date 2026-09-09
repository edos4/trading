from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns import _dedup
from patterns._rules import notional_qty
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
    HORIZON_BARS = 3
    MAX_OPEN_PER_SYMBOL = 1  # `.cjs` F10: one open flag trade per symbol
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_009_flag_pattern"
    skipped = True

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
        current = _dedup.current_bar(len(df) - 1)
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
            setup_key=(setup.pole_start, setup.pole_end, setup.flag_end),
            stop_loss=stop,
            trailing_stop_pct=0.03,
            trailing_stop_mode="highest_close",
            trailing_activation_pct=0.0,
            trailing_ref_after_check=True,  # `.cjs` flag ratchets extreme after the stop check
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
        """`.cjs` backtest_flag_final.cjs findFlags + F7 breakout. The current
        bar is the breakout: for each candidate (pole, flag) whose flag ends in
        the last 20 bars, require this bar to be the FIRST close above the flag
        high on volume >= the flag average."""
        sma = ind.sma(50)
        o = ind.open.to_numpy(dtype=float)
        c = ind.close.to_numpy(dtype=float)
        h = ind.high.to_numpy(dtype=float)
        low = ind.low.to_numpy(dtype=float)
        vol = ind.volume.to_numpy(dtype=float)
        b_close, b_vol = c[current], vol[current]

        best: _Setup | None = None
        best_pole_len = 999
        for flag_end in range(max(current - 20, 1), current):
            for flag_len in range(4, 16):
                flag_start = flag_end - flag_len + 1
                pole_end = flag_start - 1
                if pole_end < 24:  # need 20-bar baseline before the pole
                    continue
                flag_high = h[flag_start:flag_end + 1].max()
                flag_low = low[flag_start:flag_end + 1].min()
                # F7: this bar is the first breakout on volume
                if b_close <= flag_high or b_vol < vol[flag_start:flag_end + 1].mean():
                    continue
                if (c[flag_end + 1:current] > flag_high).any():
                    continue
                flag_avg = vol[flag_start:flag_end + 1].mean()

                for pole_len in range(3, 41):
                    if pole_len >= best_pole_len:
                        break
                    pole_start = pole_end - pole_len + 1
                    if pole_start - 20 < 0:
                        continue
                    pole_start_px = o[pole_start]
                    pole_end_px = c[pole_end]
                    if pole_start_px <= 0:
                        continue
                    move = (pole_end_px - pole_start_px) / pole_start_px
                    if move < 0.25:                       # F1 + C17
                        continue
                    pole_avg = vol[pole_start:pole_end + 1].mean()
                    pre_avg = vol[pole_start - 20:pole_start].mean()
                    if pre_avg <= 0 or pole_avg / pre_avg < 1.15:   # F2
                        continue
                    # C13: pre-existing uptrend
                    s0, s5 = float(sma.iloc[pole_start]), float(sma.iloc[pole_start - 5])
                    if not (s0 == s0) or not (s5 == s5):  # NaN guard
                        continue
                    if c[pole_start] < s0 or s0 < s5:
                        continue
                    # C17: flag low 10-34% below the pole end (close)
                    retrace = (pole_end_px - flag_low) / pole_end_px
                    if retrace < 0.10 or retrace > 0.34:
                        continue
                    drift = (c[flag_end] - o[flag_start]) / o[flag_start]   # F5
                    if drift > 0.06:
                        continue
                    if flag_avg / pole_avg > 0.85:        # F6
                        continue
                    best = _Setup(
                        pole_start, pole_end, flag_start, flag_end,
                        float(pole_end_px), float(flag_high), float(flag_low),
                        round(move, 4), round(pole_avg / pre_avg, 2),
                        round(retrace, 4), round(drift, 4),
                        round(flag_avg / pole_avg, 4),
                    )
                    best_pole_len = pole_len
                    break
        return best

