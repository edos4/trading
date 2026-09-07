from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns import _dedup
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

# `.cjs` pennant_find_historical.cjs (LOCKED 2026-07-03) + backtest_pennant_200.cjs
FLAG_MIN_LEN, FLAG_MAX_LEN = 3, 10
FLAG_MIN_RET = 0.10
FLAG_MIN_VOLX = 1.3
CONSOL_MIN_LEN, CONSOL_MAX_LEN = 5, 10
CONSOL_MAX_RETRACE = 0.30
CONSOL_MAX_VOLX = 0.7
BREAKOUT_MIN_VOLX = 1.5
TRAIL_PCT = 0.05
MAX_HOLD = 60


def _linreg_slope(ys: np.ndarray) -> float:
    n = len(ys)
    if n < 2:
        return 0.0
    xs = np.arange(n, dtype=float)
    xm, ym = xs.mean(), ys.mean()
    den = float(((xs - xm) ** 2).sum())
    return 0.0 if den == 0 else float(((xs - xm) * (ys - ym)).sum() / den)


@dataclass(frozen=True)
class _Setup:
    direction: str
    pole_start: int
    pole_end: int
    consol_start: int
    consol_end: int
    pole_ret: float
    retrace: float
    consol_volx: float
    breakout_volx: float


class PennantPattern(BasePattern):
    MIN_BARS = 50
    HORIZON_BARS = MAX_HOLD + 1
    MAX_OPEN_PER_SYMBOL = 1
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_010_pennant"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Continuation pennant: a >=10% high-volume impulse, a 5-10 day converging low-volume coil, and a volume-confirmed breakout in the impulse direction."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        current = _dedup.current_bar(len(df) - 1)
        if _dedup.used(current):  # `.cjs` ±3-bar breakout cluster dedup
            return None
        close = ind.close.to_numpy(dtype=float)
        high = ind.high.to_numpy(dtype=float)
        low = ind.low.to_numpy(dtype=float)
        vol = ind.volume.to_numpy(dtype=float)

        # breakout bar is the current bar; the coil ends the bar before.
        consol_end = current - 1
        best: _Setup | None = None
        for consol_len in range(CONSOL_MIN_LEN, CONSOL_MAX_LEN + 1):
            consol_start = consol_end - consol_len + 1
            for flag_len in range(FLAG_MIN_LEN, FLAG_MAX_LEN + 1):
                end_idx = consol_start - 1
                start_idx = end_idx - flag_len + 1
                if start_idx - 21 < 0:
                    continue
                pre, post = close[start_idx - 1], close[end_idx]
                if pre <= 0:
                    continue
                ret = (post - pre) / pre
                if abs(ret) < FLAG_MIN_RET:
                    continue
                direction = "bull" if ret > 0 else "bear"

                flag_vol = vol[start_idx:end_idx + 1].mean()
                prior_vol = vol[start_idx - 21:start_idx - 1].mean()
                if prior_vol <= 0 or flag_vol < FLAG_MIN_VOLX * prior_vol:
                    continue
                flag_range = high[start_idx:end_idx + 1].max() - low[start_idx:end_idx + 1].min()
                if flag_range <= 0:
                    continue

                ch = high[consol_start:consol_end + 1]
                cl = low[consol_start:consol_end + 1]
                cv = vol[consol_start:consol_end + 1]
                slope_h = _linreg_slope(ch) / post
                slope_l = _linreg_slope(cl) / post
                contraction = (ch[-1] - cl[-1]) < (ch[0] - cl[0]) * 0.7
                convergence = (slope_h - slope_l) < -0.0005
                if direction == "bull":
                    retrace = (post - cl.min()) / flag_range
                else:
                    retrace = (ch.max() - post) / flag_range
                consol_vol = cv.mean()
                vol_contraction = flag_vol > 0 and consol_vol <= CONSOL_MAX_VOLX * flag_vol
                if not (contraction and convergence and retrace <= CONSOL_MAX_RETRACE
                        and vol_contraction):
                    continue

                b_close = close[current]
                if direction == "bull" and not b_close > ch.max():
                    continue
                if direction == "bear" and not b_close < cl.min():
                    continue
                b_volx = vol[current] / consol_vol if consol_vol > 0 else 0.0
                if b_volx < BREAKOUT_MIN_VOLX:
                    continue

                cand = _Setup(direction, start_idx, end_idx, consol_start, consol_end,
                              round(ret * 100, 2), round(retrace * 100, 1),
                              round(consol_vol / flag_vol, 2), round(b_volx, 2))
                # `.cjs` keeps the tightest coil (lowest consolVolX) in a cluster
                if best is None or cand.consol_volx < best.consol_volx:
                    best = cand

        if best is None:
            return None
        price = float(close[current])
        bull = best.direction == "bull"
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY" if bull else "SELL",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=1.0,
            price=price,
            qty=self.POSITION_NOTIONAL / price if price > 0 else 0.0,
            setup_key=tuple(range(current - 3, current + 4)),  # `.cjs` ±3-bar cluster
            trailing_stop_pct=TRAIL_PCT,
            trailing_stop_mode="highest_close" if bull else "lowest_close",
            trailing_activation_pct=0.0,
            trailing_stop_on_close=True,
            exit_bars_after_entry=MAX_HOLD,
            notes=(f"{best.direction} pennant pole={best.pole_start}->{best.pole_end} "
                   f"ret={best.pole_ret}% coil={best.consol_start}->{best.consol_end} "
                   f"retrace={best.retrace}% coilVolX={best.consol_volx} bVolX={best.breakout_volx}"),
            chart_annotations=[
                ann_marker(self.bar_date(df, best.pole_start), float(low[best.pole_start] if bull else high[best.pole_start]), "pole start", ANN_REF, "o", "below" if bull else "above"),
                ann_marker(self.bar_date(df, best.pole_end), float(high[best.pole_end] if bull else low[best.pole_end]), "pole", ANN_PEAK if bull else ANN_TROUGH, "v" if bull else "^", "above" if bull else "below"),
                ann_segment(self.bar_date(df, best.pole_start), self.bar_date(df, best.pole_end), float(close[best.pole_start]), float(close[best.pole_end]), ANN_LINE),
                ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "below" if bull else "above"),
            ],
        )
