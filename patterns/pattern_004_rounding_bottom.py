"""
patterns/pattern_004_rounding_bottom.py — Rounding Bottom (saucer) long setup.

Locked rules (2026-06-25):
  Detection:
    C1 Cup depth (neckline → cup-bottom close) between 15% and 50%.
    C2 RSI-14 at cup bottom < 45 (oversold at the base).
    C3 Price higher lows on recovery — baked into the 2-day HH+HL trigger.
    C4 RSI higher lows on recovery    — baked into the 2-day HH+HL trigger.
    C5 Parabolic shape fit: least-squares quadratic on ±60 bars centered on
       the bottom; require a > 0 (concave up) AND ≥ 70% of closes within 5%
       of the fitted curve.
    C6 RSI bullish divergence or uptrend:
       primary  — any RSI local-low pair in the cup with price lower-low and
                  RSI higher-low;
       fallback A — bottom close < cup-start close AND bottom RSI > cup-start RSI;
       fallback B — ≥ 70% of bars from bottom → entry trigger have rising RSI.
  Entry: 2-day HH+HL+RSI confirmation (Day2 beats Day1 beats prior); enter on
         Day-2 close. Scan up to 120 bars after the bottom.
  Gate 2 upside: target = entry + 80% × (neckline − entry); require upside ≥ 23%.
  Trade management: $10,000/trade, initial stop 5% below entry, trailing stop
         15% below the highest high since entry, active stop = max(initial,
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
class _RoundingBottomSetup:
    bottom_idx: int
    neckline_idx: int
    entry_idx: int
    neckline: float
    bottom_close: float
    bottom_rsi: float
    cup_depth_pct: float
    shape_a: float
    shape_fit_pct: float
    divergence_kind: str   # "primary" | "fallback_a" | "fallback_b"
    upside_pct: float
    target: float


class RoundingBottomPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_004_rounding_bottom"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A rounding bottom (saucer) on a daily chart: a smooth U-shaped "
            "base where price declines into an oversold low (RSI < 45) then "
            "rounds back up toward the prior neckline. The cup depth is 15–50%, "
            "the curve is concave-up with ≥70% of closes within 5% of a fitted "
            "parabola, and RSI shows bullish divergence or a rising recovery. "
            "Entry is a LONG on the close of the second consecutive HH+HL+RSI "
            "confirmation day after the bottom."
        )

    # ── Parameters (locked ruleset) ───────────────────────────────────────────
    RSI_PERIOD              = 14
    CUP_DEPTH_MIN           = 0.15
    CUP_DEPTH_MAX           = 0.50
    BOTTOM_RSI_MAX          = 45.0
    SHAPE_WINDOW            = 60          # ±60 bars centered on bottom
    SHAPE_FIT_MIN_PCT       = 0.70
    SHAPE_TOLERANCE         = 0.05        # 5% of fitted curve
    CUP_LOOKBACK            = 150         # search window for left neckline
    ENTRY_SCAN_MAX          = 120         # bars after bottom to find trigger
    TARGET_FRACTION         = 0.80        # 80% of (neckline − entry)
    MIN_UPSIDE              = 0.23
    INITIAL_STOP_PCT        = 0.05
    TRAILING_STOP_PCT       = 0.15
    POSITION_NOTIONAL       = 10_000.0
    RSI_RISING_MIN_PCT      = 0.70        # fallback B
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

        swing_lows = self._find_swing_lows(ind.low)
        if len(swing_lows) < 1:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for bottom_idx in reversed(swing_lows):
            # Bottom must be in the past with at least one bar after it.
            if bottom_idx + 1 > cur:
                continue
            setup = self._evaluate_bottom(ind, rsi, bottom_idx, cur)
            if setup is None:
                continue
            # Only fire on the exact entry bar (Day-2 close).
            if setup.entry_idx != cur:
                continue

            confidence = self._score_confidence(setup)
            close = float(ind.close.iloc[cur])
            qty = round(self.POSITION_NOTIONAL / close, 4)

            log.info(
                f"[{self.name}] {symbol} {timeframe} | "
                f"LONG entry | bottom@{bottom_idx} neckline={setup.neckline:.4f} "
                f"depth={setup.cup_depth_pct:.1%} upside={setup.upside_pct:.1%} "
                f"shape_a={setup.shape_a:.4f} fit={setup.shape_fit_pct:.0%} "
                f"div={setup.divergence_kind} confidence={confidence:.2f}"
            )

            return TradeSignal(
                symbol=symbol,
                action="BUY",
                pattern=self.name,
                timeframe=timeframe,
                confidence=confidence,
                price=close,
                qty=qty,
                stop_loss=round(close * (1 - self.INITIAL_STOP_PCT), 4),
                take_profit=round(setup.target, 4),
                trailing_stop_pct=self.TRAILING_STOP_PCT,
                trailing_stop_mode="highest_high",
                notes=(
                    f"Rounding bottom | bottom@{bottom_idx} "
                    f"neckline={setup.neckline:.2f} depth={setup.cup_depth_pct:.1%} "
                    f"target={setup.target:.2f} upside={setup.upside_pct:.1%} | "
                    f"shape fit={setup.shape_fit_pct:.0%} div={setup.divergence_kind}"
                ),
                chart_annotations=[
                    ann_marker(self.bar_date(df, bottom_idx), float(ind.low.iloc[bottom_idx]), "bottom", ANN_TROUGH, "^", "below"),
                    ann_marker(self.bar_date(df, setup.neckline_idx), setup.neckline, "neck", ANN_PEAK, "v", "above"),
                    ann_hline(setup.neckline, "neckline", ANN_LINE),
                    ann_hline(setup.target, "target", ANN_TARGET),
                    ann_hline(close * (1 - self.INITIAL_STOP_PCT), "stop", ANN_STOP),
                    ann_marker(self.bar_date(df, setup.entry_idx), close, "entry", ANN_ENTRY, "o", "below"),
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

    def _evaluate_bottom(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        bottom_idx: int,
        cur: int,
    ) -> _RoundingBottomSetup | None:
        bottom_close = float(ind.close.iloc[bottom_idx])
        bottom_rsi   = float(rsi.iloc[bottom_idx])

        # C2: oversold at bottom.
        if not np.isfinite(bottom_rsi) or bottom_rsi >= self.BOTTOM_RSI_MAX:
            return None

        # Left neckline = highest high in the lookback before the bottom.
        neck_lo = max(0, bottom_idx - self.CUP_LOOKBACK)
        neck_hi = bottom_idx  # exclusive end
        if neck_hi - neck_lo < 2:
            return None
        neck_slice = ind.high.iloc[neck_lo:neck_hi]
        neckline_idx = neck_lo + int(neck_slice.values.argmax())
        neckline = float(ind.high.iloc[neckline_idx])

        # Bottom must be the lowest close from the neckline down to itself.
        cup_to_bottom = ind.close.iloc[neckline_idx : bottom_idx + 1]
        if cup_to_bottom.empty or bottom_close > float(cup_to_bottom.min()):
            return None

        # C1: cup depth.
        if neckline <= 0:
            return None
        cup_depth = (neckline - bottom_close) / neckline
        if cup_depth < self.CUP_DEPTH_MIN or cup_depth > self.CUP_DEPTH_MAX:
            return None

        # C5: parabolic shape fit on ±window bars centered on the bottom.
        a, fit_pct = self._parabolic_fit(ind.close, bottom_idx)
        if a is None:
            return None
        if a <= 0.0:
            return None  # not concave-up
        if fit_pct < self.SHAPE_FIT_MIN_PCT:
            return None  # too noisy

        # Entry trigger: 2-day HH+HL+RSI confirmation (C3 + C4 baked in).
        entry_idx = self._find_entry_trigger(ind, rsi, bottom_idx, cur)
        if entry_idx is None:
            return None

        # C6: RSI bullish divergence or uptrend.
        div_kind = self._rsi_divergence(ind, rsi, neckline_idx, bottom_idx, entry_idx)
        if div_kind is None:
            return None

        # Gate 2 upside + target.
        entry_close = float(ind.close.iloc[entry_idx])
        if entry_close <= 0:
            return None
        target = entry_close + self.TARGET_FRACTION * (neckline - entry_close)
        upside = (target - entry_close) / entry_close
        if upside < self.MIN_UPSIDE:
            return None

        return _RoundingBottomSetup(
            bottom_idx=bottom_idx,
            neckline_idx=neckline_idx,
            entry_idx=entry_idx,
            neckline=neckline,
            bottom_close=bottom_close,
            bottom_rsi=bottom_rsi,
            cup_depth_pct=cup_depth,
            shape_a=float(a),
            shape_fit_pct=fit_pct,
            divergence_kind=div_kind,
            upside_pct=upside,
            target=target,
        )

    def _parabolic_fit(
        self, close: pd.Series, center: int
    ) -> tuple[float | None, float]:
        """Least-squares quadratic on ±window closes centered on `center`.

        Returns (a, within_pct) where a is the x² coefficient (None if the
        window is too small) and within_pct is the fraction of closes whose
        abs deviation from the fit is ≤ SHAPE_TOLERANCE × |fit|.
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
        bottom_idx: int,
        cur: int,
    ) -> int | None:
        """First Day-2 of two consecutive HH+HL+RSI-rising bars after bottom."""
        end = min(cur, bottom_idx + self.ENTRY_SCAN_MAX)
        day1: int | None = None
        for k in range(bottom_idx + 1, end + 1):
            beats_prior = (
                float(ind.high.iloc[k]) > float(ind.high.iloc[k - 1])
                and float(ind.low.iloc[k]) > float(ind.low.iloc[k - 1])
                and float(rsi.iloc[k]) > float(rsi.iloc[k - 1])
            )
            if day1 is None:
                if beats_prior:
                    day1 = k
                continue
            beats_day1 = (
                float(ind.high.iloc[k]) > float(ind.high.iloc[day1])
                and float(ind.low.iloc[k]) > float(ind.low.iloc[day1])
                and float(rsi.iloc[k]) > float(rsi.iloc[day1])
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
        bottom_idx: int,
        entry_idx: int,
    ) -> str | None:
        # Primary: any pair of RSI local lows in the cup with price lower-low
        # and RSI higher-low.
        lows = self._rsi_local_lows(rsi, neck_idx, bottom_idx)
        for i in range(len(lows)):
            for j in range(i + 1, len(lows)):
                pi, pj = lows[i], lows[j]
                if (float(ind.close.iloc[pj]) < float(ind.close.iloc[pi])
                        and float(rsi.iloc[pj]) > float(rsi.iloc[pi])):
                    return "primary"

        # Fallback A: bottom below cup-start on price but above on RSI.
        if (float(ind.close.iloc[bottom_idx]) < float(ind.close.iloc[neck_idx])
                and float(rsi.iloc[bottom_idx]) > float(rsi.iloc[neck_idx])):
            return "fallback_a"

        # Fallback B: ≥70% rising-RSI bars from bottom → entry trigger.
        if entry_idx > bottom_idx + 1:
            rises = 0
            total = 0
            for k in range(bottom_idx + 1, entry_idx + 1):
                total += 1
                if float(rsi.iloc[k]) > float(rsi.iloc[k - 1]):
                    rises += 1
            if total and rises / total >= self.RSI_RISING_MIN_PCT:
                return "fallback_b"
        return None

    @staticmethod
    def _rsi_local_lows(
        rsi: pd.Series, start: int, end: int
    ) -> list[int]:
        out: list[int] = []
        lo = max(1, start)
        hi = min(len(rsi) - 1, end)
        for i in range(lo, hi):
            if (float(rsi.iloc[i]) < float(rsi.iloc[i - 1])
                    and float(rsi.iloc[i]) < float(rsi.iloc[i + 1])):
                out.append(i)
        return out

    def _score_confidence(self, setup: _RoundingBottomSetup) -> float:
        score = 0.55  # all hard filters passed

        # Deeper (but in-range) cups → stronger base.
        if 0.25 <= setup.cup_depth_pct <= 0.45:
            score += 0.10
        # Lower bottom RSI → more oversold.
        if setup.bottom_rsi <= 35.0:
            score += 0.10
        # Cleaner parabolic fit.
        if setup.shape_fit_pct >= 0.85:
            score += 0.10
        # Classic divergence beats fallbacks.
        if setup.divergence_kind == "primary":
            score += 0.10
        # Bigger upside than the minimum.
        if setup.upside_pct >= 0.35:
            score += 0.05

        return min(score, 1.0)
