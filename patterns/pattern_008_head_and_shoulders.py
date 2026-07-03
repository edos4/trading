"""
patterns/pattern_007_head_and_shoulders.py — Head & Shoulders top (short setup).

Locked rules (2026-06-29), 17 conditions:

  Detection
    C1   HEAD is a local close maximum (±4 bars, strict inequality).
    C2   LS is the best local close maximum in [HD−80, HD−10] bars and below HEAD.
    C3   LN depth (LS→LN) ≥ 5%: (LS − LN) / LS ≥ 0.05.
    C4   HEAD ≥ 10% above neckline: (HD − neck@HD) / neck@HD ≥ 0.10.
    C5   Neckline slope: skew ≤ +10% AND |skew| ≤ 30%  (skew = (RN−LN)/LN).
    C5b  RN depth (HD→RN) ≥ 5%: (HD − RN) / HD ≥ 0.05.
    C6   RS forms 3–50 bars after RN (MIN_RS_AFTER_RN = 3).
    C6b  RS ≥ 5% above neckline: (RS − neck@RS) / neck@RS ≥ 0.05.
    C7   RS close < LS close (right shoulder lower than left).
    C8   RSI divergence LS→HEAD ≥ 2 pts (Wilder RSI-14: lsRSI − hdRSI ≥ 2).
    C9   RSI at RS < RSI at HEAD (any amount).
    C10  RS RSI ≤ 60 (hard cap — overbought RS rejected).
    C11  Pattern span LS→RS: 20–120 bars AND RS side ≤ 2.5× LS side.
    C12  2 closes after RS both below RS close: c[rs+1] < c[rs] AND c[rs+2] < c[rs].
    C13  No close above HEAD after RS (hard invalidation — cancels pattern).

  Entry / trade management (LOCKED)
    C14  Entry = min(day7, consecBreakIdx): 2nd consecutive close below the
         neckline OR day 7 after RS, whichever is earlier.
    C15  Measured target = neck@entry − (HEAD − neck@HEAD).
    C16  Exit timer: hard close 10 bars after entry.
    C17  Trailing stop: 3% on CLOSE above running low (close-based, no intraday).

The neckline is the straight line through the left neckline low (LN, lowest low
between LS and HD) and the right neckline low (RN, lowest low between HD and RS),
interpolated to the bar being evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ann_segment,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_TARGET, ANN_ENTRY,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _HeadAndShouldersSetup:
    ls_idx: int
    ln_idx: int
    hd_idx: int
    rn_idx: int
    rs_idx: int
    neckline_at_hd: float
    neckline_at_rs: float
    neckline_at_entry: float
    ls_close: float
    hd_close: float
    rs_close: float
    ln_low: float
    rn_low: float
    skew: float
    ls_rsi: float
    hd_rsi: float
    rs_rsi: float
    span_bars: int
    entry_idx: int
    target: float


class HeadAndShouldersPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_007_head_and_shoulders"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A head & shoulders top on a daily chart: three successive peaks "
            "(left shoulder, head, right shoulder) with the head the highest. "
            "A neckline connects the low between LS and HEAD (LN) with the low "
            "between HEAD and RS (RN). RSI shows bearish divergence (lower at "
            "HEAD than at LS, and lower again at RS), the right shoulder closes "
            "below the left, and two consecutive closes break the neckline. "
            "Entry is a SHORT on the earlier of the 2nd consecutive neckline "
            "break close or day 7 after the right shoulder. Target is the "
            "neckline minus the head height."
        )

    # ── Parameters (locked 2026-06-29) ─────────────────────────────────────────
    RSI_PERIOD              = 14
    HEAD_LOOKBACK           = 4           # C1: ±4 bars, strict inequality
    LS_WINDOW_BACK          = 80          # C2: [HD−80, HD−10]
    LS_WINDOW_NEAR          = 10
    MIN_RS_AFTER_RN         = 3           # C6
    MAX_RS_AFTER_RN         = 50          # C6
    LN_DEPTH_MIN            = 0.05        # C3
    HEAD_ABOVE_NECK_MIN     = 0.10        # C4
    SKEW_MAX                = 0.10        # C5: skew ≤ +10%
    SKEW_ABS_MAX            = 0.30        # C5: |skew| ≤ 30%
    RN_DEPTH_MIN            = 0.05        # C5b
    RS_ABOVE_NECK_MIN       = 0.05        # C6b
    RSI_DIVERGENCE_MIN      = 2.0         # C8
    RS_RSI_HARD_CAP         = 60.0        # C10
    SPAN_MIN                = 20          # C11: LS→RS bars
    SPAN_MAX                = 120         # C11
    RS_SIDE_RATIO_MAX       = 2.5         # C11: RS side ≤ 2.5× LS side
    ENTRY_BARS_AFTER_RS     = 7           # C14: day 7
    EXIT_BARS_AFTER_ENTRY   = 10          # C16
    TRAILING_STOP_PCT       = 0.03        # C17: 3% on close above running low
    SHARES                  = 25
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
        rsi = ind.rsi_wilder(self.RSI_PERIOD)
        if rsi.isna().iloc[current_idx]:
            return None

        close_peaks = self._find_close_swing_highs(ind.close, self.HEAD_LOOKBACK)
        if len(close_peaks) < 2:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        # Walk HEAD candidates newest→oldest. For each, try every RS candidate
        # after it; the first that satisfies all hard filters AND whose entry
        # bar is `cur` fires the signal.
        for hd_idx in reversed(close_peaks):
            # Need at least RS + 2 confirming bars + room for entry.
            if hd_idx + self.HEAD_LOOKBACK > cur:
                continue
            setup = self._evaluate_head(ind, rsi, close_peaks, hd_idx, cur)
            if setup is None:
                continue
            if setup.entry_idx != cur:
                continue

            confidence = self._score_confidence(setup)
            close = float(ind.close.iloc[cur])

            log.info(
                f"[{self.name}] {symbol} {timeframe} | "
                f"SHORT entry | LS@{setup.ls_idx} HD@{setup.hd_idx} "
                f"RS@{setup.rs_idx} span={setup.span_bars}bars "
                f"neck@HD={setup.neckline_at_hd:.4f} skew={setup.skew:+.1%} "
                f"RSI_div={setup.ls_rsi - setup.hd_rsi:.1f} "
                f"target={setup.target:.2f} confidence={confidence:.2f}"
            )

            return TradeSignal(
                symbol=symbol,
                action="SELL",
                pattern=self.name,
                timeframe=timeframe,
                confidence=confidence,
                price=close,
                qty=self.SHARES,
                take_profit=round(setup.target, 4),
                trailing_stop_pct=self.TRAILING_STOP_PCT,
                trailing_stop_mode="lowest_close",
                neckline=setup.neckline_at_entry,
                neckline_break_direction="below",
                exit_bars_after_neckline_break=self.EXIT_BARS_AFTER_ENTRY,
                notes=(
                    f"Head & shoulders | LS@{setup.ls_idx} HD@{setup.hd_idx} "
                    f"RS@{setup.rs_idx} | span={setup.span_bars}bars | "
                    f"skew={setup.skew:+.1%} | "
                    f"LS_RSI={setup.ls_rsi:.1f} HD_RSI={setup.hd_rsi:.1f} "
                    f"RS_RSI={setup.rs_rsi:.1f} | target={setup.target:.2f}"
                ),
                chart_annotations=[
                    ann_marker(self.bar_date(df, setup.ls_idx), float(ind.high.iloc[setup.ls_idx]), "LS", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.hd_idx), float(ind.high.iloc[setup.hd_idx]), "HD", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.rs_idx), float(ind.high.iloc[setup.rs_idx]), "RS", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.ln_idx), setup.ln_low, "LN", ANN_TROUGH, "^", "below"),
                    ann_marker(self.bar_date(df, setup.rn_idx), setup.rn_low, "RN", ANN_TROUGH, "^", "below"),
                    ann_segment(self.bar_date(df, setup.ln_idx), self.bar_date(df, setup.entry_idx),
                                setup.ln_low, setup.neckline_at_entry, ANN_LINE, "-", 1.4),
                    ann_hline(setup.target, "target", ANN_TARGET),
                    ann_marker(self.bar_date(df, setup.entry_idx), close, "entry", ANN_ENTRY, "o", "above"),
                ],
            )

        return None

    # ── Detection helpers ──────────────────────────────────────────────────────
    def _find_close_swing_highs(
        self, close: pd.Series, lb: int
    ) -> list[int]:
        """Strict local close maxima with ±lb bars on each side."""
        peaks: list[int] = []
        n = len(close)
        for i in range(lb, n - lb):
            c = float(close.iloc[i])
            left = close.iloc[i - lb : i].to_numpy(dtype=float)
            right = close.iloc[i + 1 : i + lb + 1].to_numpy(dtype=float)
            if c > left.max() and c > right.max():
                peaks.append(i)
        return peaks

    def _evaluate_head(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        close_peaks: list[int],
        hd_idx: int,
        cur: int,
    ) -> _HeadAndShouldersSetup | None:

        hd_close = float(ind.close.iloc[hd_idx])
        hd_rsi = float(rsi.iloc[hd_idx])
        if not np.isfinite(hd_rsi):
            return None

        # C2: LS = best (highest-close) swing-high in [HD−80, HD−10].
        ls_lo = hd_idx - self.LS_WINDOW_BACK
        ls_hi = hd_idx - self.LS_WINDOW_NEAR  # exclusive upper bound
        ls_candidates = [i for i in close_peaks if ls_lo <= i < ls_hi]
        if not ls_candidates:
            return None
        ls_idx = max(ls_candidates, key=lambda i: float(ind.close.iloc[i]))
        ls_close = float(ind.close.iloc[ls_idx])
        if ls_close >= hd_close:  # LS must be below HEAD
            return None
        ls_rsi = float(rsi.iloc[ls_idx])
        if not np.isfinite(ls_rsi):
            return None

        # LN = lowest low strictly between LS and HD.
        ln_slice = ind.low.iloc[ls_idx + 1 : hd_idx]
        if ln_slice.empty:
            return None
        ln_idx = ls_idx + 1 + int(ln_slice.values.argmin())
        ln_low = float(ind.low.iloc[ln_idx])

        # C3: LN depth.
        if ls_close <= 0 or (ls_close - ln_low) / ls_close < self.LN_DEPTH_MIN:
            return None

        # Try every RS candidate after HD; pick the first that fully validates.
        rs_candidates = [i for i in close_peaks if i > hd_idx]
        # Newest-first so the most recent completed shoulder is preferred.
        for rs_idx in rs_candidates:
            setup = self._evaluate_rs(
                ind, rsi, ls_idx, ln_idx, ln_low, hd_idx, hd_close, hd_rsi,
                ls_close, ls_rsi, rs_idx, cur,
            )
            if setup is not None:
                return setup
        return None

    def _evaluate_rs(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        ls_idx: int,
        ln_idx: int,
        ln_low: float,
        hd_idx: int,
        hd_close: float,
        hd_rsi: float,
        ls_close: float,
        ls_rsi: float,
        rs_idx: int,
        cur: int,
    ) -> _HeadAndShouldersSetup | None:

        # RN = lowest low strictly between HD and RS.
        rn_slice = ind.low.iloc[hd_idx + 1 : rs_idx]
        if rn_slice.empty:
            return None
        rn_idx = hd_idx + 1 + int(rn_slice.values.argmin())
        rn_low = float(ind.low.iloc[rn_idx])

        # C6: RS forms 3–50 bars after RN.
        rs_after_rn = rs_idx - rn_idx
        if rs_after_rn < self.MIN_RS_AFTER_RN or rs_after_rn > self.MAX_RS_AFTER_RN:
            return None

        # C5b: RN depth.
        if hd_close <= 0 or (hd_close - rn_low) / hd_close < self.RN_DEPTH_MIN:
            return None

        # Neckline through (ln_idx, ln_low) and (rn_idx, rn_low).
        if rn_idx == ln_idx:
            return None
        slope = (rn_low - ln_low) / (rn_idx - ln_idx)

        def neck_at(bar: int) -> float:
            return ln_low + slope * (bar - ln_idx)

        neck_hd = neck_at(hd_idx)
        neck_rs = neck_at(rs_idx)

        # C4: HEAD ≥ 10% above neckline.
        if neck_hd <= 0 or (hd_close - neck_hd) / neck_hd < self.HEAD_ABOVE_NECK_MIN:
            return None

        # C5: neckline slope — skew ≤ +10% AND |skew| ≤ 30%.
        if ln_low <= 0:
            return None
        skew = (rn_low - ln_low) / ln_low
        if skew > self.SKEW_MAX or abs(skew) > self.SKEW_ABS_MAX:
            return None

        # C6b: RS ≥ 5% above neckline.
        rs_close = float(ind.close.iloc[rs_idx])
        if neck_rs <= 0 or (rs_close - neck_rs) / neck_rs < self.RS_ABOVE_NECK_MIN:
            return None

        # C7: RS close < LS close.
        if rs_close >= ls_close:
            return None

        # C8: RSI divergence LS→HEAD ≥ 2 pts.
        rs_rsi = float(rsi.iloc[rs_idx])
        if not np.isfinite(rs_rsi):
            return None
        if ls_rsi - hd_rsi < self.RSI_DIVERGENCE_MIN:
            return None

        # C9: RSI at RS < RSI at HEAD.
        if rs_rsi >= hd_rsi:
            return None

        # C10: RS RSI ≤ 60.
        if rs_rsi > self.RS_RSI_HARD_CAP:
            return None

        # C11: pattern span and side ratio.
        span = rs_idx - ls_idx
        if span < self.SPAN_MIN or span > self.SPAN_MAX:
            return None
        ls_side = hd_idx - ls_idx
        rs_side = rs_idx - hd_idx
        if ls_side <= 0 or rs_side > self.RS_SIDE_RATIO_MAX * ls_side:
            return None

        # C12: 2 closes after RS both below RS close.
        if rs_idx + 2 > cur:
            return None
        if float(ind.close.iloc[rs_idx + 1]) >= rs_close:
            return None
        if float(ind.close.iloc[rs_idx + 2]) >= rs_close:
            return None

        # C14: entry = min(day7, consecBreakIdx).
        day7_idx = rs_idx + self.ENTRY_BARS_AFTER_RS
        consec_break_idx = self._consec_neckline_break_idx(
            ind.close, ln_idx, ln_low, slope, rs_idx, cur,
        )

        if consec_break_idx is not None:
            entry_idx = min(day7_idx, consec_break_idx)
        else:
            if day7_idx > cur:
                return None
            entry_idx = day7_idx

        if entry_idx > cur or entry_idx < rs_idx + 2:
            return None

        # C13: no close above HEAD after RS up to entry (hard invalidation).
        post_rs_high = ind.close.iloc[rs_idx + 1 : entry_idx + 1]
        if not post_rs_high.empty and float(post_rs_high.max()) > hd_close:
            return None

        # C15: measured target = neck@entry − (HEAD − neck@HEAD).
        neck_entry = neck_at(entry_idx)
        head_height = hd_close - neck_hd
        target = neck_entry - head_height

        return _HeadAndShouldersSetup(
            ls_idx=ls_idx,
            ln_idx=ln_idx,
            hd_idx=hd_idx,
            rn_idx=rn_idx,
            rs_idx=rs_idx,
            neckline_at_hd=neck_hd,
            neckline_at_rs=neck_rs,
            neckline_at_entry=neck_entry,
            ls_close=ls_close,
            hd_close=hd_close,
            rs_close=rs_close,
            ln_low=ln_low,
            rn_low=rn_low,
            skew=skew,
            ls_rsi=ls_rsi,
            hd_rsi=hd_rsi,
            rs_rsi=rs_rsi,
            span_bars=span,
            entry_idx=entry_idx,
            target=target,
        )

    def _consec_neckline_break_idx(
        self,
        close: pd.Series,
        ln_idx: int,
        ln_low: float,
        slope: float,
        rs_idx: int,
        cur: int,
    ) -> int | None:
        """Index of the 2nd consecutive close below the neckline after RS.

        Neckline value at bar `i` = ln_low + slope * (i − ln_idx).
        """
        below = False
        for i in range(rs_idx + 1, cur + 1):
            neck_val = ln_low + slope * (i - ln_idx)
            if float(close.iloc[i]) < neck_val:
                if below:
                    return i  # second consecutive close below neckline
                below = True
            else:
                below = False
        return None

    def _score_confidence(self, setup: _HeadAndShouldersSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.ls_rsi - setup.hd_rsi >= 4.0:
            score += 0.10
        if setup.rs_rsi <= 55.0:
            score += 0.10
        # Symmetric shoulders in time.
        ls_side = setup.hd_idx - setup.ls_idx
        rs_side = setup.rs_idx - setup.hd_idx
        if ls_side > 0 and 0.8 <= rs_side / ls_side <= 1.25:
            score += 0.10
        # Head clearly above neckline.
        if (setup.hd_close - setup.neckline_at_hd) / setup.neckline_at_hd >= 0.15:
            score += 0.10
        # Right shoulder meaningfully below left.
        if setup.rs_close <= 0.97 * setup.ls_close:
            score += 0.05

        return min(score, 1.0)
