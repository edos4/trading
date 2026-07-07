"""
patterns/pattern_003_double_bottom.py — Double Bottom (W pattern) long setup.

Inverse of patterns/pattern_002_double_top.py:
  Detection: two swing lows (L1, L2) with bullish RSI divergence,
  peak height, volume weakness on leg 2, and no post-L2 breach before entry.
  Entry: long on bar 7 after L2 OR neckline-break bar, whichever is first.
  Exit hints on TradeSignal: take_profit 7% above neckline, trailing stop
  3% below highest close since entry, and a 5-bar exit after neckline break.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_TARGET, ANN_ENTRY,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _DoubleBottomSetup:
    l1_idx: int
    l2_idx: int
    peak_idx: int
    neckline: float
    l1_low: float
    l2_low: float
    l1_close: float
    l2_close: float
    l1_rsi: float
    l2_rsi: float
    peak_height_pct: float
    rsi_divergence: float
    entry_idx: int


class DoubleBottomPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_003_double_bottom"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A double bottom (W pattern) on a daily chart: two troughs at similar "
            "height with a peak between them. L2 low is above L1 low, RSI at L2 "
            "is higher than at L1 (bullish divergence), and L1 RSI was oversold "
            "(≤30). The second leg down shows weak volume. Entry is a LONG on "
            "day 7 after L2 or on the first close above the neckline (peak high), "
            "whichever comes first."
        )

    # ── Parameters (inverse of double_top.py) ─────────────────────────────────
    RSI_PERIOD           = 14
    L1_RSI_MAX           = 30.0
    L2_RSI_MIN           = 39.0
    L2_RSI_MAX           = 50.0
    RSI_DIVERGENCE_MIN   = 3.0
    PEAK_HEIGHT_MIN      = 0.05      # 5% rise from L1 low to peak
    L1_L2_GAP_MIN        = 8
    L1_L2_GAP_MAX        = 90
    ENTRY_BARS_AFTER_L2  = 7
    TAKE_PROFIT_ABOVE_NK = 0.07      # sell 7% above neckline
    TRAILING_STOP_PCT    = 0.03      # 3% below highest close since entry (acts as stop loss)
    EXIT_BARS_AFTER_NECK_BREAK = 5
    SWING_LOOKBACK       = 2
    MIN_BARS             = 120
    SHARES               = 25

    # ── Core logic ─────────────────────────────────────────────────────────────
    def analyze(
        self,
        snapshot: MarketSnapshot,
        store: OHLCVStore,
    ) -> TradeSignal | None:

        symbol    = snapshot.symbol
        timeframe = snapshot.timeframe
        current_idx = -1  # latest bar

        df = store.get_df(symbol, timeframe, min_bars=self.MIN_BARS)
        if df is None:
            log.debug(f"[{self.name}] {symbol} {timeframe}: not enough history yet")
            return None

        ind = IndicatorEngine(df)
        rsi = ind.rsi(self.RSI_PERIOD)
        if rsi.isna().iloc[current_idx]:
            return None

        swing_lows = self._find_swing_lows(ind.low)
        if len(swing_lows) < 2:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for l2_idx in reversed(swing_lows):
            if l2_idx + 2 > cur:
                continue  # need 2 confirming bars after L2

            l1_candidates = [i for i in swing_lows if i < l2_idx]
            for l1_idx in reversed(l1_candidates):
                setup = self._evaluate_pair(
                    ind, rsi, l1_idx, l2_idx, cur
                )
                if setup is None:
                    continue
                if setup.entry_idx != cur:
                    continue

                confidence = self._score_confidence(setup)
                close = float(ind.close.iloc[cur])

                log.info(
                    f"[{self.name}] {symbol} {timeframe} | "
                    f"LONG entry | L1@{l1_idx} L2@{l2_idx} "
                    f"neckline={setup.neckline:.4f} "
                    f"peak_height={setup.peak_height_pct:.1%} "
                    f"RSI_div={setup.rsi_divergence:.1f} "
                    f"confidence={confidence:.2f}"
                )

                return TradeSignal(
                    symbol=symbol,
                    action="BUY",
                    pattern=self.name,
                    timeframe=timeframe,
                    confidence=confidence,
                    price=close,
                    qty=self.SHARES,
                    take_profit=round(
                        setup.neckline * (1 + self.TAKE_PROFIT_ABOVE_NK), 4
                    ),
                    trailing_stop_pct=self.TRAILING_STOP_PCT,
                    trailing_stop_mode="highest_close",
                    neckline=setup.neckline,
                    neckline_break_direction="above",
                    exit_bars_after_neckline_break=self.EXIT_BARS_AFTER_NECK_BREAK,
                    notes=(
                        f"Double bottom L1→L2 gap={l2_idx - l1_idx}bars | "
                        f"neckline={setup.neckline:.2f} | "
                        f"L1_RSI={setup.l1_rsi:.1f} L2_RSI={setup.l2_rsi:.1f} | "
                        f"peak={setup.peak_height_pct:.1%}"
                    ),
                    chart_annotations=[
                        ann_marker(self.bar_date(df, l1_idx), setup.l1_low, "L1", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, l2_idx), setup.l2_low, "L2", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, setup.peak_idx), setup.neckline, "neck", ANN_PEAK, "v", "above"),
                        ann_hline(setup.neckline, "neckline", ANN_LINE),
                        ann_hline(setup.neckline * (1 + self.TAKE_PROFIT_ABOVE_NK), "TP", ANN_TARGET),
                        ann_marker(self.bar_date(df, cur), close, "entry", ANN_ENTRY, "o", "below"),
                    ],
                )

        return None

    # ── Detection helpers ──────────────────────────────────────────────────────
    def _find_swing_lows(self, low: pd.Series) -> list[int]:
        lb = self.SWING_LOOKBACK
        troughs: list[int] = []
        for i in range(lb, len(low) - lb):
            left  = low.iloc[i - lb : i]
            right = low.iloc[i + 1 : i + lb + 1]
            if low.iloc[i] <= left.min() and low.iloc[i] <= right.min():
                troughs.append(i)
        return troughs

    def _evaluate_pair(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        l1_idx: int,
        l2_idx: int,
        cur: int,
    ) -> _DoubleBottomSetup | None:
        gap = l2_idx - l1_idx
        if gap < self.L1_L2_GAP_MIN or gap > self.L1_L2_GAP_MAX:
            return None

        l1_low   = float(ind.low.iloc[l1_idx])
        l2_low   = float(ind.low.iloc[l2_idx])
        l1_close = float(ind.close.iloc[l1_idx])
        l2_close = float(ind.close.iloc[l2_idx])
        l1_rsi   = float(rsi.iloc[l1_idx])
        l2_rsi   = float(rsi.iloc[l2_idx])

        if l2_low <= l1_low:
            return None
        if l1_rsi > self.L1_RSI_MAX:
            return None
        if l2_rsi < self.L2_RSI_MIN:
            return None
        if l2_rsi > self.L2_RSI_MAX:
            return None
        if l2_rsi <= l1_rsi:
            return None
        rsi_div = l2_rsi - l1_rsi
        if rsi_div <= self.RSI_DIVERGENCE_MIN:
            return None
        if l2_close <= l1_close:
            return None

        # No bar between L1 and L2 undercuts L1 low.
        between_low = ind.low.iloc[l1_idx + 1 : l2_idx]
        if not between_low.empty and between_low.min() < l1_low:
            return None

        # Peak between troughs.
        peak_slice = ind.high.iloc[l1_idx + 1 : l2_idx]
        if peak_slice.empty:
            return None
        peak_rel = int(peak_slice.values.argmax())
        peak_idx = l1_idx + 1 + peak_rel
        peak_high = float(ind.high.iloc[peak_idx])

        peak_height = (peak_high - l1_low) / l1_low
        if peak_height < self.PEAK_HEIGHT_MIN:
            return None

        # Next 2 bars after L2 close higher.
        if l2_idx + 2 > cur:
            return None
        if float(ind.close.iloc[l2_idx + 1]) <= l2_close:
            return None
        if float(ind.close.iloc[l2_idx + 2]) <= l2_close:
            return None

        # Leg 2 (peak → L2): weak selloff volume.
        if not self._leg2_volume_weak(ind, peak_idx, l2_idx):
            return None

        neckline = peak_high
        neck_break_idx = self._neckline_break_idx(ind.close, l2_idx, cur, neckline)
        day7_idx = l2_idx + self.ENTRY_BARS_AFTER_L2
        if neck_break_idx is not None:
            entry_idx = min(day7_idx, neck_break_idx)
        else:
            if day7_idx > cur:
                return None
            entry_idx = day7_idx

        # C13 (inverse): cancel if any bar after L2 breaches L2 low before the
        # neckline break. When entry is the neckline break this is l2+1..neck-1;
        # when day-7 entry fires first the neckline has not broken yet by cur,
        # so we monitor every available bar up to cur. (Checking up to entry
        # is equivalent here because entry == min(day7, neck_break).)
        post_l2_end = neck_break_idx if neck_break_idx is not None else cur
        post_l2 = ind.low.iloc[l2_idx + 1 : post_l2_end]
        if not post_l2.empty and post_l2.min() < l2_low:
            return None

        return _DoubleBottomSetup(
            l1_idx=l1_idx,
            l2_idx=l2_idx,
            peak_idx=peak_idx,
            neckline=neckline,
            l1_low=l1_low,
            l2_low=l2_low,
            l1_close=l1_close,
            l2_close=l2_close,
            l1_rsi=l1_rsi,
            l2_rsi=l2_rsi,
            peak_height_pct=peak_height,
            rsi_divergence=rsi_div,
            entry_idx=entry_idx,
        )

    def _leg2_volume_weak(
        self, ind: IndicatorEngine, peak_idx: int, l2_idx: int
    ) -> bool:
        """Avg DOWN-bar volume < avg UP-bar volume on leg 2."""
        up_vols: list[float] = []
        down_vols: list[float] = []
        for i in range(peak_idx, l2_idx + 1):
            vol = float(ind.volume.iloc[i])
            if float(ind.close.iloc[i]) > float(ind.open.iloc[i]):
                up_vols.append(vol)
            elif float(ind.close.iloc[i]) < float(ind.open.iloc[i]):
                down_vols.append(vol)
        if not up_vols or not down_vols:
            return False
        return sum(down_vols) / len(down_vols) < sum(up_vols) / len(up_vols)

    def _neckline_break_idx(
        self,
        close: pd.Series,
        l2_idx: int,
        cur: int,
        neckline: float,
    ) -> int | None:
        for i in range(l2_idx + 1, cur + 1):
            if float(close.iloc[i]) > neckline:
                return i
        return None

    def _score_confidence(self, setup: _DoubleBottomSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.peak_height_pct >= 0.07:
            score += 0.10
        if setup.rsi_divergence >= 5.0:
            score += 0.10
        if setup.l2_rsi >= 42.0:
            score += 0.10
        gap = setup.l2_idx - setup.l1_idx
        if 15 <= gap <= 60:
            score += 0.10
        low_ratio = setup.l2_low / setup.l1_low
        if low_ratio >= 1.02:
            score += 0.05

        return min(score, 1.0)
