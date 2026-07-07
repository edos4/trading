"""
patterns/pattern_005_rounding_top.py — Rounding Top (inverted saucer) short setup.

Inverse of patterns/pattern_004_rounding_bottom.py:
  Detection:
    C1 Dome depth (top close → neckline low) between 15% and 50%.
    C2 RSI-14 at the dome top > 55 (overbought at the crown).
    C3 Price lower highs on decline — baked into the 2-day LH+LL trigger.
    C4 RSI lower highs on decline    — baked into the 2-day LH+LL trigger.
    C5 Parabolic shape fit: least-squares quadratic on ±60 bars centered on
       the top; require a < 0 (concave down / inverted-U) AND ≥ 70% of closes
       within 5% of the fitted curve.
    C6 RSI bearish divergence or downtrend:
       primary  — any RSI local-high pair in the dome with price higher-high
                  and RSI lower-high;
       fallback A — top close > cup-start close AND top RSI < cup-start RSI;
       fallback B — ≥ 70% of bars from top → entry trigger have falling RSI.
  Entry: 2-day LH+LL+RSI-falling confirmation (Day2 beats Day1 beats prior);
         enter SHORT on Day-2 close. Scan up to 120 bars after the top.
  Gate 2 downside: target = entry − 80% × (entry − neckline); require downside
         ≥ 23%.
  Trade management: $10,000/trade, initial stop 5% above entry, trailing stop
         15% above the lowest low since entry, active stop = min(initial,
         trailing), target as above. Hold until stop or target only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_TARGET, ANN_ENTRY, ANN_STOP,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _RoundingTopSetup:
    top_idx: int
    neckline_idx: int
    entry_idx: int
    neckline: float
    top_close: float
    top_rsi: float
    dome_depth_pct: float
    shape_a: float
    shape_fit_pct: float
    divergence_kind: str   # "primary" | "fallback_a" | "fallback_b"
    downside_pct: float
    target: float


class RoundingTopPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_005_rounding_top"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A rounding top (inverted saucer) on a daily chart: a smooth "
            "∩-shaped dome where price rises into an overbought high (RSI > 55) "
            "then rolls back down toward the prior neckline low. The dome depth "
            "is 15–50%, the curve is concave-down with ≥70% of closes within 5% "
            "of a fitted parabola, and RSI shows bearish divergence or a falling "
            "decline. Entry is a SHORT on the close of the second consecutive "
            "LH+LL+RSI-falling confirmation day after the top."
        )

    # ── Parameters (inverse of rounding_bottom.py) ────────────────────────────
    RSI_PERIOD              = 14
    DOME_DEPTH_MIN          = 0.15
    DOME_DEPTH_MAX          = 0.50
    TOP_RSI_MIN             = 55.0       # inverse of BOTTOM_RSI_MAX = 45
    SHAPE_WINDOW            = 60         # ±60 bars centered on top
    SHAPE_FIT_MIN_PCT       = 0.70
    SHAPE_TOLERANCE         = 0.05
    CUP_LOOKBACK            = 150        # search window for left neckline low
    ENTRY_SCAN_MAX          = 120        # bars after top to find trigger
    TARGET_FRACTION         = 0.80       # 80% of (entry − neckline)
    MIN_DOWNSIDE            = 0.23
    INITIAL_STOP_PCT        = 0.05
    TRAILING_STOP_PCT       = 0.15
    TRAILING_ACTIVATION_PCT = 0.05
    POSITION_NOTIONAL       = 10_000.0
    RSI_FALLING_MIN_PCT     = 0.70       # fallback B
    SWING_LOOKBACK          = 2
    MIN_BARS                = 200

    # ── Core logic ─────────────────────────────────────────────────────────────
    def analyze(
        self,
        snapshot: MarketSnapshot,
        store: OHLCVStore,
    ) -> TradeSignal | None:

        symbol    = snapshot.symbol
        timeframe = snapshot.timeframe
        current_idx = -1

        df = store.get_df(symbol, timeframe, min_bars=self.MIN_BARS)
        if df is None:
            log.debug(f"[{self.name}] {symbol} {timeframe}: not enough history yet")
            return None

        ind = IndicatorEngine(df)
        rsi = ind.rsi(self.RSI_PERIOD)
        if rsi.isna().iloc[current_idx]:
            return None

        swing_highs = self._find_swing_highs(ind.high)
        if len(swing_highs) < 1:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for top_idx in reversed(swing_highs):
            if top_idx + 1 > cur:
                continue
            setup = self._evaluate_top(ind, rsi, top_idx, cur)
            if setup is None:
                continue
            if setup.entry_idx != cur:
                continue

            confidence = self._score_confidence(setup)
            close = float(ind.close.iloc[cur])
            qty = round(self.POSITION_NOTIONAL / close, 4)

            log.info(
                f"[{self.name}] {symbol} {timeframe} | "
                f"SHORT entry | top@{top_idx} neckline={setup.neckline:.4f} "
                f"depth={setup.dome_depth_pct:.1%} downside={setup.downside_pct:.1%} "
                f"shape_a={setup.shape_a:.4f} fit={setup.shape_fit_pct:.0%} "
                f"div={setup.divergence_kind} confidence={confidence:.2f}"
            )

            return TradeSignal(
                symbol=symbol,
                action="SELL",
                pattern=self.name,
                timeframe=timeframe,
                confidence=confidence,
                price=close,
                qty=qty,
                stop_loss=round(close * (1 + self.INITIAL_STOP_PCT), 4),
                take_profit=round(setup.target, 4),
                trailing_stop_pct=self.TRAILING_STOP_PCT,
                trailing_stop_mode="lowest_low",
                trailing_activation_pct=self.TRAILING_ACTIVATION_PCT,
                notes=(
                    f"Rounding top | top@{top_idx} "
                    f"neckline={setup.neckline:.2f} depth={setup.dome_depth_pct:.1%} "
                    f"target={setup.target:.2f} downside={setup.downside_pct:.1%} | "
                    f"shape fit={setup.shape_fit_pct:.0%} div={setup.divergence_kind}"
                ),
                chart_annotations=[
                    ann_marker(self.bar_date(df, top_idx), float(ind.high.iloc[top_idx]), "top", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.neckline_idx), setup.neckline, "neck", ANN_TROUGH, "^", "below"),
                    ann_hline(setup.neckline, "neckline", ANN_LINE),
                    ann_hline(setup.target, "target", ANN_TARGET),
                    ann_hline(close * (1 + self.INITIAL_STOP_PCT), "stop", ANN_STOP),
                    ann_marker(self.bar_date(df, setup.entry_idx), close, "entry", ANN_ENTRY, "o", "above"),
                ],
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

    def _evaluate_top(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        top_idx: int,
        cur: int,
    ) -> _RoundingTopSetup | None:
        top_close = float(ind.close.iloc[top_idx])
        top_rsi   = float(rsi.iloc[top_idx])

        # C2: overbought at the crown.
        if not np.isfinite(top_rsi) or top_rsi <= self.TOP_RSI_MIN:
            return None

        # Left neckline = lowest low in the lookback before the top.
        neck_lo = max(0, top_idx - self.CUP_LOOKBACK)
        neck_hi = top_idx  # exclusive end
        if neck_hi - neck_lo < 2:
            return None
        neck_slice = ind.low.iloc[neck_lo:neck_hi]
        neckline_idx = neck_lo + int(neck_slice.values.argmin())
        neckline = float(ind.low.iloc[neckline_idx])

        # Top must be the highest close from the neckline up to itself.
        cup_to_top = ind.close.iloc[neckline_idx : top_idx + 1]
        if cup_to_top.empty or top_close < float(cup_to_top.max()):
            return None

        # C1: dome depth (rise from neckline low to top close).
        if neckline <= 0:
            return None
        dome_depth = (top_close - neckline) / neckline
        if dome_depth < self.DOME_DEPTH_MIN or dome_depth > self.DOME_DEPTH_MAX:
            return None

        # C5: parabolic shape fit on ±window bars centered on the top.
        a, fit_pct = self._parabolic_fit(ind.close, top_idx)
        if a is None:
            return None
        if a >= 0.0:
            return None  # not concave-down
        if fit_pct < self.SHAPE_FIT_MIN_PCT:
            return None  # too noisy

        # Entry trigger: 2-day LH+LL+RSI-falling (C3 + C4 baked in).
        entry_idx = self._find_entry_trigger(ind, rsi, top_idx, cur)
        if entry_idx is None:
            return None

        # C6: RSI bearish divergence or downtrend.
        div_kind = self._rsi_divergence(ind, rsi, neckline_idx, top_idx, entry_idx)
        if div_kind is None:
            return None

        # Gate 2 downside + target.
        entry_close = float(ind.close.iloc[entry_idx])
        if entry_close <= 0:
            return None
        target = entry_close - self.TARGET_FRACTION * (entry_close - neckline)
        downside = (entry_close - target) / entry_close
        if downside < self.MIN_DOWNSIDE:
            return None

        return _RoundingTopSetup(
            top_idx=top_idx,
            neckline_idx=neckline_idx,
            entry_idx=entry_idx,
            neckline=neckline,
            top_close=top_close,
            top_rsi=top_rsi,
            dome_depth_pct=dome_depth,
            shape_a=float(a),
            shape_fit_pct=fit_pct,
            divergence_kind=div_kind,
            downside_pct=downside,
            target=target,
        )

    def _parabolic_fit(
        self, close: pd.Series, center: int
    ) -> tuple[float | None, float]:
        """Least-squares quadratic on ±window closes centered on `center`.

        Returns (a, within_pct): a is the x² coefficient (None if the window
        is too small) and within_pct is the fraction of closes whose abs
        deviation from the fit is ≤ SHAPE_TOLERANCE × |fit|.
        """
        w = self.SHAPE_WINDOW
        lo = max(0, center - w)
        hi = min(len(close), center + w + 1)
        if hi - lo < 3:
            return None, 0.0
        x = np.arange(lo, hi) - center
        y = close.iloc[lo:hi].to_numpy(dtype=float)
        try:
            a, b, c = np.polyfit(x, y, 2)
        except (np.linalg.LinAlgError, ValueError):
            return None, 0.0
        fit = a * x * x + b * x + c
        with np.errstate(divide="ignore", invalid="ignore"):
            rel = np.abs(y - fit) / np.abs(fit)
        within = float(np.mean(np.nan_to_num(rel, nan=1.0) <= self.SHAPE_TOLERANCE))
        return float(a), within

    def _find_entry_trigger(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        top_idx: int,
        cur: int,
    ) -> int | None:
        """First Day-2 of two consecutive LH+LL+RSI-falling bars after top."""
        end = min(cur, top_idx + self.ENTRY_SCAN_MAX)
        day1: int | None = None
        for k in range(top_idx + 1, end + 1):
            beats_prior = (
                float(ind.high.iloc[k]) < float(ind.high.iloc[k - 1])
                and float(ind.low.iloc[k]) < float(ind.low.iloc[k - 1])
                and float(rsi.iloc[k]) < float(rsi.iloc[k - 1])
            )
            if day1 is None:
                if beats_prior:
                    day1 = k
                continue
            beats_day1 = (
                float(ind.high.iloc[k]) < float(ind.high.iloc[day1])
                and float(ind.low.iloc[k]) < float(ind.low.iloc[day1])
                and float(rsi.iloc[k]) < float(rsi.iloc[day1])
            )
            if beats_day1:
                return k
            # Sequence broke → rescan, this bar may start a new Day 1.
            day1 = k if beats_prior else None
        return None

    def _rsi_divergence(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        neck_idx: int,
        top_idx: int,
        entry_idx: int,
    ) -> str | None:
        # Primary: any pair of RSI local highs in the dome with price
        # higher-high and RSI lower-high.
        highs = self._rsi_local_highs(rsi, neck_idx, top_idx)
        for i in range(len(highs)):
            for j in range(i + 1, len(highs)):
                pi, pj = highs[i], highs[j]
                if (float(ind.close.iloc[pj]) > float(ind.close.iloc[pi])
                        and float(rsi.iloc[pj]) < float(rsi.iloc[pi])):
                    return "primary"

        # Fallback A: top above cup-start on price but below on RSI.
        if (float(ind.close.iloc[top_idx]) > float(ind.close.iloc[neck_idx])
                and float(rsi.iloc[top_idx]) < float(rsi.iloc[neck_idx])):
            return "fallback_a"

        # Fallback B: ≥70% falling-RSI bars from top → entry trigger.
        if entry_idx > top_idx + 1:
            falls = 0
            total = 0
            for k in range(top_idx + 1, entry_idx + 1):
                total += 1
                if float(rsi.iloc[k]) < float(rsi.iloc[k - 1]):
                    falls += 1
            if total and falls / total >= self.RSI_FALLING_MIN_PCT:
                return "fallback_b"
        return None

    @staticmethod
    def _rsi_local_highs(
        rsi: pd.Series, start: int, end: int
    ) -> list[int]:
        out: list[int] = []
        lo = max(1, start)
        hi = min(len(rsi) - 1, end)
        for i in range(lo, hi):
            if (float(rsi.iloc[i]) > float(rsi.iloc[i - 1])
                    and float(rsi.iloc[i]) > float(rsi.iloc[i + 1])):
                out.append(i)
        return out

    def _score_confidence(self, setup: _RoundingTopSetup) -> float:
        score = 0.55  # all hard filters passed

        if 0.25 <= setup.dome_depth_pct <= 0.45:
            score += 0.10
        if setup.top_rsi >= 65.0:
            score += 0.10
        if setup.shape_fit_pct >= 0.85:
            score += 0.10
        if setup.divergence_kind == "primary":
            score += 0.10
        if setup.downside_pct >= 0.35:
            score += 0.05

        return min(score, 1.0)
