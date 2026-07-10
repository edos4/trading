"""
patterns/pattern_010_pennant.py — Pennant continuation setup (bidirectional).

Locked ruleset from patterns/pennant.md (LOCKED 2026-07-05). A sharp impulse
move (the flagpole) is followed by a short, coiling consolidation between two
CONVERGING trendlines (the pennant — converging is what separates it from a
flag, whose lines are parallel). Price then breaks out in the direction of the
flagpole. Works long (bullish pennant) or short (bearish pennant).

Gate — Flagpole (the impulse move):
  G1  Sharp move ≥ 10% over ≤ 10 trading days, either direction.
  G2  Flagpole avg volume ≥ 1.3× the 20-day average preceding it.

Consolidation (the pennant itself), C1–C6:
  C1  Starts within 1–2 bars of the flagpole extreme (peak / trough).
  C2  Duration 5–10 trading days (short-and-sharp is the real tell).
  C3  Converging trendlines: upper (swing highs) and lower (swing lows) slope
      toward each other — true convergence, not parallel.
  C4  No close outside the trendlines until the breakout bar.
  C5  Retrace ≤ 30% of the flagpole range (shallower = stronger continuation).
  C6  Volume contraction ≤ 70% of the flagpole avg (the coiling tell).

Breakout / entry, C7–C8 (C9 RSI-confirm was tested and REJECTED):
  C7  Close beyond the trendline, in the same direction as the flagpole.
  C8  Breakout volume ≥ 1.5× the consolidation average.

Exit — 5% close-based trailing stop:
  Entry at the breakout-bar close. Track the extreme close since entry (highest
  for long, lowest for short); exit when a close breaches 5% off that extreme.
  No fixed target, no time stop (5% beat 3%/7%, measured-move and hybrid
  variants in testing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ann_segment,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_ENTRY, ANN_REF,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _PennantSetup:
    direction: Literal["bull", "bear"]
    pole_start_idx: int
    pole_extreme_idx: int
    cons_start_idx: int
    cons_end_idx: int
    entry_idx: int
    flagpole_range: float
    pole_move_pct: float
    pole_vol_ratio: float       # pole avg vol / 20-day baseline (G2)
    retrace_pct: float          # C5
    cons_vol_ratio: float       # cons avg vol / pole avg vol (C6)
    breakout_vol_ratio: float   # breakout vol / cons avg vol (C8)
    upper_slope: float
    lower_slope: float
    breakout_line: float        # trendline value at the breakout bar


class PennantPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_010_pennant"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A pennant on a daily chart: a sharp impulse move (the flagpole, "
            "≥10% in ≤10 days on heavy volume) followed by a short 5–10 bar "
            "consolidation whose upper and lower trendlines CONVERGE toward a "
            "point (a small symmetrical triangle), on contracting volume. Entry "
            "is in the direction of the flagpole on a volume-backed close beyond "
            "the converging trendline — LONG for a bullish pennant, SHORT for a "
            "bearish one."
        )

    # ── Parameters (from pennant.md) ─────────────────────────────────────────────
    POLE_MAX_BARS         = 10           # G1: move over ≤10 trading days
    POLE_MOVE_MIN         = 0.10         # G1: ≥10%
    POLE_VOL_EXPANSION    = 1.30         # G2: pole vol ≥ 1.3× baseline
    VOL_BASELINE_BARS     = 20           # G2: 20-day pre-pole baseline
    CONS_START_MAX_OFFSET = 2            # C1: starts within 1–2 bars of extreme
    CONS_LEN_MIN          = 5            # C2: 5–10 bars
    CONS_LEN_MAX          = 10
    CONVERGENCE_MAX_RATIO = 0.80         # C3: end width ≤ 80% of start width
    TREND_TOL_PCT         = 0.02         # C4: close-outside tolerance (2%)
    RETRACE_MAX           = 0.30         # C5: retrace ≤ 30% of pole range
    CONS_VOL_MAX          = 0.70         # C6: cons vol ≤ 70% of pole vol
    BREAKOUT_VOL_MIN      = 1.50         # C8: breakout vol ≥ 1.5× cons vol
    TRAILING_STOP_PCT     = 0.05         # 5% close-based trailing stop
    SWING_LOOKBACK        = 2
    MIN_BARS              = 80
    POSITION_NOTIONAL     = 10_000.0

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

        swing_highs = self._find_swings(ind.high, kind="high")
        swing_lows  = self._find_swings(ind.low, kind="low")

        # Bullish pennant: flagpole extreme is a swing high; breakout up.
        for p1 in reversed(swing_highs):
            setup = self._evaluate(ind, "bull", p1, cur)
            if setup is not None:
                return self._build_signal(df, ind, symbol, timeframe, setup, cur)

        # Bearish pennant: flagpole extreme is a swing low; breakout down.
        for p1 in reversed(swing_lows):
            setup = self._evaluate(ind, "bear", p1, cur)
            if setup is not None:
                return self._build_signal(df, ind, symbol, timeframe, setup, cur)

        return None

    # ── Detection helpers ──────────────────────────────────────────────────────
    def _find_swings(self, series: pd.Series, kind: str) -> list[int]:
        lb = self.SWING_LOOKBACK
        out: list[int] = []
        for i in range(lb, len(series) - lb):
            left  = series.iloc[i - lb : i]
            right = series.iloc[i + 1 : i + lb + 1]
            if kind == "high":
                if series.iloc[i] >= left.max() and series.iloc[i] >= right.max():
                    out.append(i)
            else:
                if series.iloc[i] <= left.min() and series.iloc[i] <= right.min():
                    out.append(i)
        return out

    def _evaluate(
        self,
        ind: IndicatorEngine,
        direction: str,
        p1: int,
        cur: int,
    ) -> _PennantSetup | None:
        # The consolidation runs up to the bar before the breakout (cur).
        cons_end = cur - 1
        if cons_end <= p1:
            return None

        # G1 + G2: strongest qualifying flagpole ending at the extreme p1.
        pole = self._find_flagpole(ind, direction, p1)
        if pole is None:
            return None
        pole_start, pole_move, flagpole_range, pole_vol_ratio, pole_avg_vol = pole
        if flagpole_range <= 0 or pole_avg_vol <= 0:
            return None

        # C1 + C2: consolidation starts within 1–2 bars of the extreme and lasts
        # 5–10 bars, ending the bar before the breakout.
        for offset in range(0, self.CONS_START_MAX_OFFSET + 1):
            cons_start = p1 + offset
            cons_len = cons_end - cons_start + 1
            if cons_len < self.CONS_LEN_MIN or cons_len > self.CONS_LEN_MAX:
                continue

            setup = self._check_consolidation(
                ind, direction, pole_start, p1, cons_start, cons_end, cur,
                flagpole_range, pole_move, pole_vol_ratio, pole_avg_vol,
            )
            if setup is not None:
                return setup
        return None

    def _find_flagpole(
        self,
        ind: IndicatorEngine,
        direction: str,
        p1: int,
    ) -> tuple[int, float, float, float, float] | None:
        """Strongest ≤10-bar flagpole ending at the extreme p1.

        Returns (pole_start, move_pct, flagpole_range, vol_ratio, pole_avg_vol).
        """
        best: tuple[int, float, float, float, float] | None = None
        best_move = -1.0

        for pole_len in range(2, self.POLE_MAX_BARS + 1):
            pole_start = p1 - pole_len + 1
            if pole_start < 0:
                break
            base_lo = pole_start - self.VOL_BASELINE_BARS
            if base_lo < 0:
                continue

            if direction == "bull":
                base_price = float(ind.low.iloc[pole_start])
                rng = float(ind.high.iloc[p1]) - base_price
            else:
                base_price = float(ind.high.iloc[pole_start])
                rng = base_price - float(ind.low.iloc[p1])
            if base_price <= 0 or rng <= 0:
                continue
            move = rng / base_price
            if move < self.POLE_MOVE_MIN:
                continue

            baseline = float(ind.volume.iloc[base_lo:pole_start].mean())
            pole_avg_vol = float(ind.volume.iloc[pole_start : p1 + 1].mean())
            if baseline <= 0:
                continue
            vol_ratio = pole_avg_vol / baseline
            if vol_ratio < self.POLE_VOL_EXPANSION:
                continue

            if move > best_move:
                best_move = move
                best = (pole_start, move, rng, vol_ratio, pole_avg_vol)

        return best

    def _check_consolidation(
        self,
        ind: IndicatorEngine,
        direction: str,
        pole_start: int,
        p1: int,
        cons_start: int,
        cons_end: int,
        cur: int,
        flagpole_range: float,
        pole_move: float,
        pole_vol_ratio: float,
        pole_avg_vol: float,
    ) -> _PennantSetup | None:
        xs = np.arange(cons_start, cons_end + 1, dtype=float)
        highs = ind.high.iloc[cons_start : cons_end + 1].to_numpy(dtype=float)
        lows  = ind.low.iloc[cons_start : cons_end + 1].to_numpy(dtype=float)
        closes = ind.close.iloc[cons_start : cons_end + 1].to_numpy(dtype=float)
        if len(xs) < 3:
            return None

        try:
            su, iu = np.polyfit(xs, highs, 1)   # upper trendline (swing highs)
            sl, il = np.polyfit(xs, lows, 1)     # lower trendline (swing lows)
        except (np.linalg.LinAlgError, ValueError):
            return None

        def upper(k: float) -> float:
            return su * k + iu

        def lower(k: float) -> float:
            return sl * k + il

        # C3: converging trendlines (not parallel). Width must shrink across the
        # consolidation and the slopes must lean toward each other.
        width_start = upper(cons_start) - lower(cons_start)
        width_end   = upper(cons_end) - lower(cons_end)
        if width_start <= 0 or width_end <= 0:
            return None
        if width_end > width_start * self.CONVERGENCE_MAX_RATIO:
            return None
        if su >= sl:  # lines must converge (upper falling relative to lower)
            return None

        # C4: no close outside the trendlines during the consolidation.
        for j, k in enumerate(xs):
            tol = self.TREND_TOL_PCT * closes[j]
            if closes[j] > upper(k) + tol or closes[j] < lower(k) - tol:
                return None

        # C5: retrace ≤ 30% of the flagpole range.
        if direction == "bull":
            cons_low = float(ind.low.iloc[cons_start : cons_end + 1].min())
            retrace = (float(ind.high.iloc[p1]) - cons_low) / flagpole_range
        else:
            cons_high = float(ind.high.iloc[cons_start : cons_end + 1].max())
            retrace = (cons_high - float(ind.low.iloc[p1])) / flagpole_range
        if retrace > self.RETRACE_MAX:
            return None

        # C6: volume contraction during the consolidation.
        cons_avg_vol = float(ind.volume.iloc[cons_start : cons_end + 1].mean())
        cons_vol_ratio = cons_avg_vol / pole_avg_vol
        if cons_vol_ratio > self.CONS_VOL_MAX:
            return None

        # C7: breakout close beyond the trendline in the flagpole's direction.
        breakout_close = float(ind.close.iloc[cur])
        if direction == "bull":
            line_at_cur = upper(cur)
            if breakout_close <= line_at_cur:
                return None
        else:
            line_at_cur = lower(cur)
            if breakout_close >= line_at_cur:
                return None

        # C8: breakout volume ≥ 1.5× the consolidation average.
        if cons_avg_vol <= 0:
            return None
        breakout_vol_ratio = float(ind.volume.iloc[cur]) / cons_avg_vol
        if breakout_vol_ratio < self.BREAKOUT_VOL_MIN:
            return None

        return _PennantSetup(
            direction=direction,
            pole_start_idx=pole_start,
            pole_extreme_idx=p1,
            cons_start_idx=cons_start,
            cons_end_idx=cons_end,
            entry_idx=cur,
            flagpole_range=flagpole_range,
            pole_move_pct=pole_move,
            pole_vol_ratio=pole_vol_ratio,
            retrace_pct=retrace,
            cons_vol_ratio=cons_vol_ratio,
            breakout_vol_ratio=breakout_vol_ratio,
            upper_slope=float(su),
            lower_slope=float(sl),
            breakout_line=float(line_at_cur),
        )

    # ── Signal construction ──────────────────────────────────────────────────────
    def _build_signal(
        self,
        df: pd.DataFrame,
        ind: IndicatorEngine,
        symbol: str,
        timeframe: str,
        setup: _PennantSetup,
        cur: int,
    ) -> TradeSignal:
        close = float(ind.close.iloc[cur])
        qty = round(self.POSITION_NOTIONAL / close, 4)
        is_bull = setup.direction == "bull"
        action: Literal["BUY", "SELL"] = "BUY" if is_bull else "SELL"
        trail_mode = "highest_close" if is_bull else "lowest_close"

        pole_extreme_price = (
            float(ind.high.iloc[setup.pole_extreme_idx]) if is_bull
            else float(ind.low.iloc[setup.pole_extreme_idx])
        )
        pole_start_price = (
            float(ind.low.iloc[setup.pole_start_idx]) if is_bull
            else float(ind.high.iloc[setup.pole_start_idx])
        )

        log.info(
            f"[{self.name}] {symbol} {timeframe} | "
            f"{action} {setup.direction} pennant | "
            f"pole {setup.pole_start_idx}->{setup.pole_extreme_idx} "
            f"move={setup.pole_move_pct:.1%} pole_vol={setup.pole_vol_ratio:.2f}x | "
            f"cons {setup.cons_start_idx}->{setup.cons_end_idx} "
            f"retrace={setup.retrace_pct:.1%} cons_vol={setup.cons_vol_ratio:.2f}x | "
            f"breakout_vol={setup.breakout_vol_ratio:.2f}x "
            f"confidence={self._score_confidence(setup):.2f}"
        )

        pole_marker_color = ANN_PEAK if is_bull else ANN_TROUGH
        return TradeSignal(
            symbol=symbol,
            action=action,
            pattern=self.name,
            timeframe=timeframe,
            confidence=self._score_confidence(setup),
            price=close,
            qty=qty,
            take_profit=None,                 # no fixed target — trail only
            trailing_stop_pct=self.TRAILING_STOP_PCT,
            trailing_stop_mode=trail_mode,
            notes=(
                f"{setup.direction.capitalize()} pennant | "
                f"pole move={setup.pole_move_pct:.1%} "
                f"({setup.pole_start_idx}->{setup.pole_extreme_idx}) "
                f"pole_vol={setup.pole_vol_ratio:.2f}x | "
                f"cons {setup.cons_start_idx}->{setup.cons_end_idx} "
                f"retrace={setup.retrace_pct:.1%} "
                f"cons_vol={setup.cons_vol_ratio:.2f}x | "
                f"breakout_vol={setup.breakout_vol_ratio:.2f}x"
            ),
            chart_annotations=[
                ann_marker(self.bar_date(df, setup.pole_start_idx), pole_start_price, "pole start", ANN_REF, "o", "below" if is_bull else "above"),
                ann_marker(self.bar_date(df, setup.pole_extreme_idx), pole_extreme_price, "pole", pole_marker_color, "v" if is_bull else "^", "above" if is_bull else "below"),
                ann_segment(self.bar_date(df, setup.pole_start_idx), self.bar_date(df, setup.pole_extreme_idx),
                            pole_start_price, pole_extreme_price, ANN_LINE, "-", 1.4),
                ann_hline(setup.breakout_line, "breakout", ANN_LINE),
                ann_marker(self.bar_date(df, setup.entry_idx), close, "entry", ANN_ENTRY, "o", "below" if is_bull else "above"),
            ],
        )

    # ── Confidence ─────────────────────────────────────────────────────────────
    def _score_confidence(self, setup: _PennantSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.pole_move_pct >= 0.20:
            score += 0.10          # a genuinely sharp flagpole
        if setup.pole_vol_ratio >= 2.0:
            score += 0.10          # heavy volume on the impulse
        if setup.cons_vol_ratio <= 0.50:
            score += 0.10          # decisive coiling / volume dry-up
        if setup.retrace_pct <= 0.20:
            score += 0.10          # shallow pullback = strong continuation
        if setup.breakout_vol_ratio >= 2.0:
            score += 0.05          # emphatic breakout

        return min(score, 1.0)
