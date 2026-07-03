"""
patterns/pattern_002_double_top.py — Double Top (M pattern) short setup.

Rules from patterns/double_top.md:
  Detection C1–C13: two swing highs (H1, H2) with bearish RSI divergence,
  valley depth, volume weakness on leg 2, and no post-H2 breach before entry.
  Entry C14: short on bar 7 after H2 OR neckline-break bar, whichever is first.
  Exit hints on TradeSignal: take_profit 7% below neckline, trailing stop
  3% above lowest close since entry, and a 5-bar exit after neckline break.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns.base_pattern import BasePattern, TradeSignal
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _DoubleTopSetup:
    h1_idx: int
    h2_idx: int
    valley_idx: int
    neckline: float
    h1_high: float
    h2_high: float
    h1_close: float
    h2_close: float
    h1_rsi: float
    h2_rsi: float
    valley_depth_pct: float
    rsi_divergence: float
    entry_idx: int


class DoubleTopPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_002_double_top"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A double top (M pattern) on a daily chart: two peaks at similar height "
            "with a valley between them. H2 high is below H1 high, RSI at H2 is lower "
            "than at H1 (bearish divergence), and H1 RSI was overbought (≥70). "
            "The second leg up shows weak volume. Entry is a SHORT on day 7 after H2 "
            "or on the first close below the neckline (valley low), whichever comes first."
        )

    # ── Parameters (from double_top.md) ──────────────────────────────────────
    RSI_PERIOD           = 14
    H1_RSI_MIN           = 70.0
    H2_RSI_MIN           = 50.0
    H2_RSI_MAX           = 61.0
    RSI_DIVERGENCE_MIN   = 3.0
    VALLEY_DEPTH_MIN     = 0.05      # 5% drop from H1 high to valley
    H1_H2_GAP_MIN        = 8
    H1_H2_GAP_MAX        = 90
    ENTRY_BARS_AFTER_H2  = 7
    TAKE_PROFIT_BELOW_NK = 0.07      # cover 7% below neckline
    TRAILING_STOP_PCT    = 0.03      # 3% above lowest close since entry
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

        swing_highs = self._find_swing_highs(ind.high)
        if len(swing_highs) < 2:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for h2_idx in reversed(swing_highs):
            if h2_idx + 2 > cur:
                continue  # C9: need 2 confirming bars after H2

            h1_candidates = [i for i in swing_highs if i < h2_idx]
            for h1_idx in reversed(h1_candidates):
                setup = self._evaluate_pair(
                    ind, rsi, h1_idx, h2_idx, cur
                )
                if setup is None:
                    continue
                if setup.entry_idx != cur:
                    continue

                confidence = self._score_confidence(setup)
                close = float(ind.close.iloc[cur])

                log.info(
                    f"[{self.name}] {symbol} {timeframe} | "
                    f"SHORT entry | H1@{h1_idx} H2@{h2_idx} "
                    f"neckline={setup.neckline:.4f} "
                    f"valley_depth={setup.valley_depth_pct:.1%} "
                    f"RSI_div={setup.rsi_divergence:.1f} "
                    f"confidence={confidence:.2f}"
                )

                return TradeSignal(
                    symbol=symbol,
                    action="SELL",
                    pattern=self.name,
                    timeframe=timeframe,
                    confidence=confidence,
                    price=close,
                    qty=self.SHARES,
                    take_profit=round(
                        setup.neckline * (1 - self.TAKE_PROFIT_BELOW_NK), 4
                    ),
                    trailing_stop_pct=self.TRAILING_STOP_PCT,
                    trailing_stop_mode="lowest_close",
                    neckline=setup.neckline,
                    neckline_break_direction="below",
                    exit_bars_after_neckline_break=self.EXIT_BARS_AFTER_NECK_BREAK,
                    notes=(
                        f"Double top H1→H2 gap={h2_idx - h1_idx}bars | "
                        f"neckline={setup.neckline:.2f} | "
                        f"H1_RSI={setup.h1_rsi:.1f} H2_RSI={setup.h2_rsi:.1f} | "
                        f"valley={setup.valley_depth_pct:.1%}"
                    ),
                )

        return None

    # ── Detection helpers ──────────────────────────────────────────────────────
    def _find_swing_highs(self, high: pd.Series) -> list[int]:
        lb = self.SWING_LOOKBACK
        peaks: list[int] = []
        for i in range(lb, len(high) - lb):
            left  = high.iloc[i - lb : i]
            right = high.iloc[i + 1 : i + lb + 1]
            if high.iloc[i] >= left.max() and high.iloc[i] >= right.max():
                peaks.append(i)
        return peaks

    def _evaluate_pair(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        h1_idx: int,
        h2_idx: int,
        cur: int,
    ) -> _DoubleTopSetup | None:
        gap = h2_idx - h1_idx
        if gap < self.H1_H2_GAP_MIN or gap > self.H1_H2_GAP_MAX:
            return None  # C3

        h1_high  = float(ind.high.iloc[h1_idx])
        h2_high  = float(ind.high.iloc[h2_idx])
        h1_close = float(ind.close.iloc[h1_idx])
        h2_close = float(ind.close.iloc[h2_idx])
        h1_rsi   = float(rsi.iloc[h1_idx])
        h2_rsi   = float(rsi.iloc[h2_idx])

        if h2_high >= h1_high:
            return None  # C4
        if h1_rsi < self.H1_RSI_MIN:
            return None  # C5
        if h2_rsi < self.H2_RSI_MIN:
            return None  # C6
        if h2_rsi > self.H2_RSI_MAX:
            return None  # C11
        if h2_rsi >= h1_rsi:
            return None  # C2
        rsi_div = h1_rsi - h2_rsi
        if rsi_div <= self.RSI_DIVERGENCE_MIN:
            return None  # C8
        if h2_close >= h1_close:
            return None  # C12

        # C7: no bar between H1 and H2 exceeds H1 high
        between_high = ind.high.iloc[h1_idx + 1 : h2_idx]
        if not between_high.empty and between_high.max() > h1_high:
            return None

        # Valley between peaks
        valley_slice = ind.low.iloc[h1_idx + 1 : h2_idx]
        if valley_slice.empty:
            return None
        valley_rel = int(valley_slice.values.argmin())
        valley_idx = h1_idx + 1 + valley_rel
        valley_low = float(ind.low.iloc[valley_idx])

        valley_depth = (h1_high - valley_low) / h1_high
        if valley_depth < self.VALLEY_DEPTH_MIN:
            return None  # C1

        # C9: next 2 bars after H2 close lower
        if h2_idx + 2 > cur:
            return None
        if float(ind.close.iloc[h2_idx + 1]) >= h2_close:
            return None
        if float(ind.close.iloc[h2_idx + 2]) >= h2_close:
            return None

        # C10: leg 2 (valley → H2) — weak recovery volume
        if not self._leg2_volume_weak(ind, valley_idx, h2_idx):
            return None

        neckline = valley_low
        neck_break_idx = self._neckline_break_idx(ind.close, h2_idx, cur, neckline)
        day7_idx = h2_idx + self.ENTRY_BARS_AFTER_H2
        if neck_break_idx is not None:
            entry_idx = min(day7_idx, neck_break_idx)
        else:
            if day7_idx > cur:
                return None
            entry_idx = day7_idx

        # C13: cancel if any bar after H2 breaches H2 high before the neckline
        # break. When entry is the neckline break this is h2+1..neck_break-1;
        # when day-7 entry fires first the neckline has not broken yet by cur,
        # so we monitor every available bar up to cur. (Checking up to entry
        # is equivalent here because entry == min(day7, neck_break).)
        post_h2_end = neck_break_idx if neck_break_idx is not None else cur
        post_h2 = ind.high.iloc[h2_idx + 1 : post_h2_end]
        if not post_h2.empty and post_h2.max() > h2_high:
            return None

        return _DoubleTopSetup(
            h1_idx=h1_idx,
            h2_idx=h2_idx,
            valley_idx=valley_idx,
            neckline=neckline,
            h1_high=h1_high,
            h2_high=h2_high,
            h1_close=h1_close,
            h2_close=h2_close,
            h1_rsi=h1_rsi,
            h2_rsi=h2_rsi,
            valley_depth_pct=valley_depth,
            rsi_divergence=rsi_div,
            entry_idx=entry_idx,
        )

    def _leg2_volume_weak(
        self, ind: IndicatorEngine, valley_idx: int, h2_idx: int
    ) -> bool:
        """C10: avg UP-bar volume < avg DOWN-bar volume on leg 2."""
        up_vols: list[float] = []
        down_vols: list[float] = []
        for i in range(valley_idx, h2_idx + 1):
            vol = float(ind.volume.iloc[i])
            if float(ind.close.iloc[i]) > float(ind.open.iloc[i]):
                up_vols.append(vol)
            elif float(ind.close.iloc[i]) < float(ind.open.iloc[i]):
                down_vols.append(vol)
        if not up_vols or not down_vols:
            return False
        return sum(up_vols) / len(up_vols) < sum(down_vols) / len(down_vols)

    def _neckline_break_idx(
        self,
        close: pd.Series,
        h2_idx: int,
        cur: int,
        neckline: float,
    ) -> int | None:
        for i in range(h2_idx + 1, cur + 1):
            if float(close.iloc[i]) < neckline:
                return i
        return None

    def _score_confidence(self, setup: _DoubleTopSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.valley_depth_pct >= 0.07:
            score += 0.10
        if setup.rsi_divergence >= 5.0:
            score += 0.10
        if setup.h2_rsi <= 58.0:
            score += 0.10
        gap = setup.h2_idx - setup.h1_idx
        if 15 <= gap <= 60:
            score += 0.10
        height_ratio = setup.h2_high / setup.h1_high
        if height_ratio <= 0.98:
            score += 0.05

        return min(score, 1.0)
