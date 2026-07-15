"""
patterns/pattern_011_breakout_retest.py — Range Breakout + Retest LONG setup.

DRAFT ruleset from patterns/breakout_retest.md — transcribed from a whiteboard
trading explainer, NOT backtested. Long only (the source only shows the
bullish case). A horizontal consolidation range breaks to the upside, price
pulls back to retest the broken resistance without closing back below it, and
a bullish confirmation candle triggers the entry.

Detection (R1–R9):
  R1 + R2  Range window: up to 90 bars before the breakout, spanning ≥10 bars
           from first touch to breakout.
  R3       Resistance = range window's highest high, with ≥2 swing-high
           touches within 1.5% of that level.
  R4       Support = range window's lowest low, with ≥2 swing-low touches
           within 1.5% of that level.
  R5       Range tightness: (resistance − support) / resistance ≤ 15%.
  R6       Breakout bar: first close above resistance — no earlier bar in the
           range closed above it.
  R7       Retest window (≤8 bars after breakout): the lowest low of the
           post-breakout bars comes within 2% above resistance.
  R8       Hold: every close from the breakout bar through the confirmation
           bar stays ≥ resistance × 0.99 (a close below this is the fakeout
           the video crosses out — no trade).
  R9       Confirmation bar (≤5 bars after the retest low): bullish
           (close > open), closes above the retest bar's high, and closes
           above resistance. Entry at this bar's close (R10).

Exit:
  Stop = retest low × 0.99.
  Target = entry + (resistance − support) — the range height projected above
  the breakout (measured move).
  Trailing stop: 3% below the highest close since entry, activating after a
  4% gain, so a strong continuation can run past the measured-move target.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ann_segment,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_STOP, ANN_TARGET, ANN_ENTRY, ANN_REF,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _BreakoutRetestSetup:
    range_start_idx: int
    breakout_idx: int
    retest_idx: int
    entry_idx: int
    resistance: float
    support: float
    resistance_touches: int
    support_touches: int
    range_height_pct: float
    retest_depth_pct: float     # how close the retest low got to resistance
    breakout_thrust_pct: float  # how far the breakout close cleared resistance
    confirm_body_pct: float     # confirmation candle's body size
    skipped: true


class BreakoutRetestPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_011_breakout_retest"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A horizontal range on a daily chart: resistance and support "
            "lines each touched at least twice, spanning at least 10 bars. "
            "Price closes above resistance (the breakout), pulls back to "
            "retest that line within 8 bars without closing back below it, "
            "then a bullish candle closes above the retest bar's high. Entry "
            "is a LONG on that confirmation bar's close."
        )

    # ── Parameters (from breakout_retest.md) ────────────────────────────────────
    RANGE_LOOKBACK_BARS  = 90     # R1
    RANGE_MIN_SPAN_BARS  = 10     # R2
    TOUCH_TOL_PCT        = 0.015  # R3 / R4
    MIN_TOUCHES          = 2      # R3 / R4
    RANGE_HEIGHT_MAX_PCT = 0.15   # R5
    RETEST_WINDOW_MAX    = 8      # R7
    RETEST_TOL_PCT       = 0.02   # R7
    HOLD_TOL_PCT         = 0.01   # R8
    CONFIRM_WINDOW_MAX   = 5      # R9
    STOP_BUFFER_PCT      = 0.01
    TRAILING_STOP_PCT    = 0.03
    TRAIL_ACTIVATION_PCT = 0.04
    SWING_LOOKBACK       = 2
    MIN_BARS             = 130
    POSITION_NOTIONAL    = 10_000.0

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
        n = len(df)
        cur = n + current_idx  # last bar index

        max_dist = self.RETEST_WINDOW_MAX + self.CONFIRM_WINDOW_MAX
        for breakout_idx in reversed(range(max(0, cur - max_dist), cur - 1)):
            setup = self._evaluate(ind, breakout_idx, cur)
            if setup is None:
                continue

            confidence = self._score_confidence(setup)
            close = float(ind.close.iloc[cur])
            qty = round(self.POSITION_NOTIONAL / close, 4)

            retest_low = float(ind.low.iloc[setup.retest_idx])
            stop = round(retest_low * (1 - self.STOP_BUFFER_PCT), 4)
            range_height_abs = setup.resistance - setup.support
            take_profit = round(close + range_height_abs, 4)

            log.info(
                f"[{self.name}] {symbol} {timeframe} | "
                f"LONG entry | range_start={setup.range_start_idx} "
                f"resistance={setup.resistance:.2f} support={setup.support:.2f} "
                f"height={setup.range_height_pct:.1%} "
                f"breakout@{setup.breakout_idx} retest@{setup.retest_idx} | "
                f"stop={stop:.2f} tp={take_profit:.2f} confidence={confidence:.2f}"
            )

            return TradeSignal(
                symbol=symbol,
                action="BUY",
                pattern=self.name,
                timeframe=timeframe,
                confidence=confidence,
                price=close,
                qty=qty,
                stop_loss=stop,
                take_profit=take_profit,
                trailing_stop_pct=self.TRAILING_STOP_PCT,
                trailing_stop_mode="highest_close",
                trailing_activation_pct=self.TRAIL_ACTIVATION_PCT,
                notes=(
                    f"Breakout+retest | resistance={setup.resistance:.2f} "
                    f"support={setup.support:.2f} height={setup.range_height_pct:.1%} | "
                    f"breakout@{setup.breakout_idx} retest@{setup.retest_idx} "
                    f"depth={setup.retest_depth_pct:.1%} | "
                    f"stop={stop:.2f} tp={take_profit:.2f}"
                ),
                chart_annotations=[
                    ann_marker(self.bar_date(df, setup.range_start_idx), setup.resistance, "range start", ANN_REF, "o", "above"),
                    ann_hline(setup.resistance, "resistance", ANN_LINE),
                    ann_hline(setup.support, "support", ANN_LINE),
                    ann_marker(self.bar_date(df, setup.breakout_idx), float(ind.close.iloc[setup.breakout_idx]), "breakout", ANN_PEAK, "^", "above"),
                    ann_marker(self.bar_date(df, setup.retest_idx), retest_low, "retest", ANN_TROUGH, "^", "below"),
                    ann_segment(self.bar_date(df, setup.breakout_idx), self.bar_date(df, setup.retest_idx),
                                setup.resistance, setup.resistance, ANN_LINE, "--", 1.2),
                    ann_hline(stop, "stop", ANN_STOP),
                    ann_hline(take_profit, "TP", ANN_TARGET),
                    ann_marker(self.bar_date(df, cur), close, "entry", ANN_ENTRY, "o", "below"),
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

    def _find_swing_lows(self, low: pd.Series) -> list[int]:
        lb = self.SWING_LOOKBACK
        troughs: list[int] = []
        for i in range(lb, len(low) - lb):
            left  = low.iloc[i - lb : i]
            right = low.iloc[i + 1 : i + lb + 1]
            if low.iloc[i] <= left.min() and low.iloc[i] <= right.min():
                troughs.append(i)
        return troughs

    def _find_range(
        self, ind: IndicatorEngine, breakout_idx: int
    ) -> tuple[int, float, float, int, int] | None:
        """R1–R5: resistance/support with ≥2 touches each, tight enough range.

        Returns (range_start_idx, resistance, support, res_touches, sup_touches).
        """
        win_start = max(0, breakout_idx - self.RANGE_LOOKBACK_BARS)
        if breakout_idx - win_start < self.RANGE_MIN_SPAN_BARS:
            return None

        high = ind.high.iloc[win_start:breakout_idx]
        low  = ind.low.iloc[win_start:breakout_idx]
        if high.empty or low.empty:
            return None

        resistance = float(high.max())
        support = float(low.min())
        if resistance <= 0 or support <= 0 or resistance <= support:
            return None

        # R5: range tightness.
        height_pct = (resistance - support) / resistance
        if height_pct > self.RANGE_HEIGHT_MAX_PCT:
            return None

        swing_highs = [i for i in self._find_swing_highs(ind.high) if win_start <= i < breakout_idx]
        swing_lows  = [i for i in self._find_swing_lows(ind.low) if win_start <= i < breakout_idx]

        res_touch_idxs = [i for i in swing_highs if float(ind.high.iloc[i]) >= resistance * (1 - self.TOUCH_TOL_PCT)]
        sup_touch_idxs = [i for i in swing_lows if float(ind.low.iloc[i]) <= support * (1 + self.TOUCH_TOL_PCT)]

        # R3 / R4: at least 2 touches on each boundary.
        if len(res_touch_idxs) < self.MIN_TOUCHES or len(sup_touch_idxs) < self.MIN_TOUCHES:
            return None

        range_start_idx = min(res_touch_idxs + sup_touch_idxs)
        if breakout_idx - range_start_idx < self.RANGE_MIN_SPAN_BARS:
            return None

        return (range_start_idx, resistance, support, len(res_touch_idxs), len(sup_touch_idxs))

    def _evaluate(
        self,
        ind: IndicatorEngine,
        breakout_idx: int,
        cur: int,
    ) -> _BreakoutRetestSetup | None:
        range_setup = self._find_range(ind, breakout_idx)
        if range_setup is None:
            return None
        range_start_idx, resistance, support, res_touches, sup_touches = range_setup

        # R6: breakout is the FIRST close above resistance in the range window.
        prior_closes = ind.close.iloc[range_start_idx:breakout_idx]
        if not prior_closes.empty and float(prior_closes.max()) > resistance:
            return None
        breakout_close = float(ind.close.iloc[breakout_idx])
        if breakout_close <= resistance:
            return None

        # R7: retest — lowest low of the post-breakout window must come back
        # within RETEST_TOL_PCT of resistance.
        post = ind.low.iloc[breakout_idx + 1 : cur + 1]
        if post.empty:
            return None
        retest_idx = breakout_idx + 1 + int(post.values.argmin())
        retest_low = float(ind.low.iloc[retest_idx])
        if retest_idx - breakout_idx > self.RETEST_WINDOW_MAX:
            return None
        if retest_low > resistance * (1 + self.RETEST_TOL_PCT):
            return None
        if cur - retest_idx > self.CONFIRM_WINDOW_MAX:
            return None
        if retest_idx >= cur:
            return None

        # R8: hold — every close from breakout through cur stays above the
        # tolerance band. A close that breaches this is the fakeout the video
        # crosses out.
        hold_slice = ind.close.iloc[breakout_idx : cur + 1]
        if float(hold_slice.min()) < resistance * (1 - self.HOLD_TOL_PCT):
            return None

        # R9: confirmation bar at cur — bullish, breaks the retest bar's high,
        # closes back above resistance.
        confirm_open = float(ind.open.iloc[cur])
        confirm_close = float(ind.close.iloc[cur])
        retest_high = float(ind.high.iloc[retest_idx])
        if confirm_close <= confirm_open:
            return None
        if confirm_close <= retest_high:
            return None
        if confirm_close <= resistance:
            return None

        height_pct = (resistance - support) / resistance
        retest_depth = (retest_low - resistance) / resistance  # <= 0 if below, small +ve if above
        breakout_thrust = (breakout_close - resistance) / resistance
        confirm_body = (confirm_close - confirm_open) / confirm_open

        return _BreakoutRetestSetup(
            range_start_idx=range_start_idx,
            breakout_idx=breakout_idx,
            retest_idx=retest_idx,
            entry_idx=cur,
            resistance=resistance,
            support=support,
            resistance_touches=res_touches,
            support_touches=sup_touches,
            range_height_pct=height_pct,
            retest_depth_pct=retest_depth,
            breakout_thrust_pct=breakout_thrust,
            confirm_body_pct=confirm_body,
        )

    # ── Confidence ─────────────────────────────────────────────────────────────
    def _score_confidence(self, setup: _BreakoutRetestSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.range_height_pct <= 0.08:
            score += 0.10          # a genuinely tight range
        if setup.resistance_touches + setup.support_touches >= 5:
            score += 0.10          # well-tested level
        if abs(setup.retest_depth_pct) <= 0.005:
            score += 0.10          # shallow, precise retest
        if setup.confirm_body_pct >= 0.01:
            score += 0.10          # decisive confirmation candle
        if setup.breakout_thrust_pct >= 0.01:
            score += 0.05          # clean initial breakout, not a marginal poke

        return min(score, 1.0)
