"""
patterns/pattern_009_flag_pattern.py — Bull Flag continuation LONG setup.

Locked ruleset from patterns/flag_pattern.md (LOCKED 2026-07-06, long only —
bear flags were tested and rejected). A violent, quality-tested rally (the
pole) pauses in a tight, low-volume consolidation (the flag) that retraces
only 10–34% of the pole's height, on top of a pre-existing 50-day-SMA uptrend.
Enter on a volume-confirmed breakout above the flag high.

Pattern recognition (F1–F7, C13, C17):
  F1 + C17  Flagpole thrust: close-vs-open gain ≥ 25% over a 3–40 bar window.
            (25%, not Bulkowski's 90%, because this is a mega/large-cap
            universe — see flag_pattern.md.)
  F2        Pole volume expansion: pole avg volume ≥ 1.15× the 20-bar baseline
            immediately before the pole.
  C17       Flag quality (supersedes the old F3): flag low sits 10–34% below
            the pole's high (Bulkowski high-tight-flag retracement band).
  F4        Flag length: 4–15 bars.
  F5        Flag drift discipline: flag net drift ≤ +6% (a pause, not a second
            leg up).
  F6        Volume dry-up: flag avg volume ≤ 0.85× pole avg volume.
  C13       Pre-existing trend: at pole start, close ≥ 50-day SMA AND that SMA
            rising vs. 5 bars earlier.
  F7        Entry trigger: first close above the flag high within 20 bars of
            flag end, with that day's volume ≥ flag average.
  F10       Only one open trade per symbol at a time (enforced by the
            scanner/backtester position manager, not here).

Exit / risk management (F8–F9, C18):
  F8 + C18  Initial stop = max(flag low, entry × 0.97) — whichever is tighter.
            Caps worst-case initial risk at 3% regardless of flag geometry.
  F9        Trailing stop: track the highest CLOSE since entry; the stop
            ratchets up to 3% below that peak and never loosens.

There is deliberately no fixed take-profit target — winners are left to run
under the trailing stop (the partial-exit variant was tested and rejected).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ann_segment,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_STOP, ANN_ENTRY, ANN_REF,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _FlagSetup:
    pole_start_idx: int
    pole_high_idx: int
    flag_start_idx: int
    flag_end_idx: int
    entry_idx: int
    pole_high: float
    pole_gain_pct: float
    pole_vol_ratio: float       # pole avg vol / 20-bar baseline (F2)
    flag_high: float
    flag_low: float
    flag_low_depth_pct: float   # how far flag low sits below pole high (C17)
    flag_drift_pct: float       # net drift across the flag (F5)
    flag_vol_ratio: float       # flag avg vol / pole avg vol (F6)


class FlagPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_009_flag_pattern"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A bull flag on a daily chart: a steep, high-volume rally (the pole, "
            "≥25% over 3–40 bars) followed by a short, tight, low-volume pullback "
            "(the flag, 4–15 bars) that only retraces 10–34% of the pole's height "
            "and drifts sideways-to-down. The move sits on top of a rising 50-day "
            "SMA. Entry is a LONG on the first close above the flag high, on volume "
            "at or above the flag's average."
        )

    # ── Parameters (from flag_pattern.md) ───────────────────────────────────────
    POLE_MIN_BARS         = 3            # F1: 3–40 bar pole window
    POLE_MAX_BARS         = 40
    POLE_GAIN_MIN         = 0.25         # F1 + C17: ≥25% close-vs-open thrust
    POLE_VOL_EXPANSION    = 1.15         # F2: pole vol ≥ 1.15× baseline
    VOL_BASELINE_BARS     = 20           # F2: 20-bar pre-pole baseline
    FLAG_LOW_DEPTH_MIN    = 0.10         # C17: flag low 10–34% below pole high
    FLAG_LOW_DEPTH_MAX    = 0.34
    FLAG_LEN_MIN          = 4            # F4: 4–15 bar flag
    FLAG_LEN_MAX          = 15
    FLAG_DRIFT_MAX        = 0.06         # F5: net drift ≤ +6%
    FLAG_VOL_MAX          = 0.85         # F6: flag vol ≤ 0.85× pole vol
    TREND_SMA             = 50           # C13: 50-day SMA
    TREND_SMA_LOOKBACK    = 5            # C13: SMA rising vs 5 bars earlier
    BREAKOUT_WINDOW       = 20           # F7: breakout within 20 bars of flag end
    INITIAL_STOP_PCT      = 0.03         # C18: entry × 0.97 hard cap
    TRAILING_STOP_PCT     = 0.03         # F9: 3% below highest close
    SWING_LOOKBACK        = 2
    MIN_BARS              = 120
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
        sma = ind.sma(self.TREND_SMA)

        swing_highs = self._find_swing_highs(ind.high)
        if not swing_highs:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for pole_high_idx in reversed(swing_highs):
            # Distance from the pole high to the breakout (cur) must be able to
            # hold a 4–15 bar flag plus a ≤20-bar breakout window.
            dist = cur - pole_high_idx
            if dist < self.FLAG_LEN_MIN + 1:
                continue
            if dist > self.FLAG_LEN_MAX + self.BREAKOUT_WINDOW:
                break  # peaks only get older going back — nothing left to find

            setup = self._evaluate(ind, sma, pole_high_idx, cur)
            if setup is None:
                continue

            confidence = self._score_confidence(setup)
            close = float(ind.close.iloc[cur])
            qty = round(self.POSITION_NOTIONAL / close, 4)

            # C18: initial stop = max(flag low, entry × 0.97) — the tighter one.
            stop = round(max(setup.flag_low, close * (1 - self.INITIAL_STOP_PCT)), 4)

            log.info(
                f"[{self.name}] {symbol} {timeframe} | "
                f"LONG entry | pole {setup.pole_start_idx}->{setup.pole_high_idx} "
                f"gain={setup.pole_gain_pct:.1%} pole_vol={setup.pole_vol_ratio:.2f}x | "
                f"flag {setup.flag_start_idx}->{setup.flag_end_idx} "
                f"depth={setup.flag_low_depth_pct:.1%} drift={setup.flag_drift_pct:+.1%} "
                f"flag_vol={setup.flag_vol_ratio:.2f}x | "
                f"stop={stop:.2f} confidence={confidence:.2f}"
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
                take_profit=None,               # winners run under the trail (F9)
                trailing_stop_pct=self.TRAILING_STOP_PCT,
                trailing_stop_mode="highest_close",
                notes=(
                    f"Bull flag | pole gain={setup.pole_gain_pct:.1%} "
                    f"({setup.pole_start_idx}->{setup.pole_high_idx}) "
                    f"pole_vol={setup.pole_vol_ratio:.2f}x | "
                    f"flag {setup.flag_start_idx}->{setup.flag_end_idx} "
                    f"depth={setup.flag_low_depth_pct:.1%} "
                    f"drift={setup.flag_drift_pct:+.1%} "
                    f"flag_vol={setup.flag_vol_ratio:.2f}x | "
                    f"flag_high={setup.flag_high:.2f} stop={stop:.2f}"
                ),
                chart_annotations=[
                    ann_marker(self.bar_date(df, setup.pole_start_idx), float(ind.low.iloc[setup.pole_start_idx]), "pole start", ANN_REF, "^", "below"),
                    ann_marker(self.bar_date(df, setup.pole_high_idx), setup.pole_high, "pole high", ANN_PEAK, "v", "above"),
                    ann_segment(self.bar_date(df, setup.pole_start_idx), self.bar_date(df, setup.pole_high_idx),
                                float(ind.low.iloc[setup.pole_start_idx]), setup.pole_high, ANN_LINE, "-", 1.4),
                    ann_marker(self.bar_date(df, setup.flag_end_idx), setup.flag_low, "flag low", ANN_TROUGH, "^", "below"),
                    ann_hline(setup.flag_high, "flag high", ANN_LINE),
                    ann_hline(stop, "stop", ANN_STOP),
                    ann_marker(self.bar_date(df, setup.entry_idx), close, "entry", ANN_ENTRY, "o", "below"),
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

    def _evaluate(
        self,
        ind: IndicatorEngine,
        sma: pd.Series,
        pole_high_idx: int,
        cur: int,
    ) -> _FlagSetup | None:
        pole_high = float(ind.high.iloc[pole_high_idx])
        if pole_high <= 0:
            return None

        # F1 + F2 + C13: find the strongest qualifying pole ending at pole_high.
        pole = self._find_pole(ind, sma, pole_high_idx)
        if pole is None:
            return None
        pole_start_idx, pole_gain, pole_vol_ratio, pole_avg_vol = pole

        flag_start = pole_high_idx + 1

        # F4: try each candidate flag length; take the first fully-valid flag
        # whose breakout lands exactly on `cur`.
        for flag_len in range(self.FLAG_LEN_MIN, self.FLAG_LEN_MAX + 1):
            flag_end = pole_high_idx + flag_len
            if flag_end >= cur:
                break  # flag would overrun the breakout bar
            # F7: breakout must be within BREAKOUT_WINDOW bars of flag end.
            if cur - flag_end > self.BREAKOUT_WINDOW:
                continue

            flag_high_s = ind.high.iloc[flag_start : flag_end + 1]
            flag_low_s  = ind.low.iloc[flag_start : flag_end + 1]
            if flag_high_s.empty:
                continue
            flag_high = float(flag_high_s.max())
            flag_low  = float(flag_low_s.min())

            # C17: flag low sits 10–34% below the pole high.
            depth = (pole_high - flag_low) / pole_high
            if depth < self.FLAG_LOW_DEPTH_MIN or depth > self.FLAG_LOW_DEPTH_MAX:
                continue

            # F5: flag net drift ≤ +6% (close at flag end vs close at flag start).
            flag_start_close = float(ind.close.iloc[flag_start])
            flag_end_close   = float(ind.close.iloc[flag_end])
            if flag_start_close <= 0:
                continue
            drift = (flag_end_close - flag_start_close) / flag_start_close
            if drift > self.FLAG_DRIFT_MAX:
                continue

            # F6: flag avg volume ≤ 0.85× pole avg volume.
            flag_avg_vol = float(ind.volume.iloc[flag_start : flag_end + 1].mean())
            if pole_avg_vol <= 0:
                continue
            flag_vol_ratio = flag_avg_vol / pole_avg_vol
            if flag_vol_ratio > self.FLAG_VOL_MAX:
                continue

            # F7: `cur` is the FIRST close above the flag high after flag end,
            # on volume ≥ the flag average.
            if not self._is_first_breakout(ind, flag_end, cur, flag_high):
                continue
            if float(ind.volume.iloc[cur]) < flag_avg_vol:
                continue

            return _FlagSetup(
                pole_start_idx=pole_start_idx,
                pole_high_idx=pole_high_idx,
                flag_start_idx=flag_start,
                flag_end_idx=flag_end,
                entry_idx=cur,
                pole_high=pole_high,
                pole_gain_pct=pole_gain,
                pole_vol_ratio=pole_vol_ratio,
                flag_high=flag_high,
                flag_low=flag_low,
                flag_low_depth_pct=depth,
                flag_drift_pct=drift,
                flag_vol_ratio=flag_vol_ratio,
            )
        return None

    def _find_pole(
        self,
        ind: IndicatorEngine,
        sma: pd.Series,
        pole_high_idx: int,
    ) -> tuple[int, float, float, float] | None:
        """Strongest 3–40 bar pole ending at `pole_high_idx`.

        Returns (pole_start_idx, gain_pct, vol_ratio, pole_avg_vol) for the
        window that maximizes the close-vs-open gain, provided it satisfies
        F1 (gain), F2 (volume expansion) and C13 (SMA uptrend at pole start).
        """
        end_close = float(ind.close.iloc[pole_high_idx])
        best: tuple[int, float, float, float] | None = None
        best_gain = -1.0

        for pole_len in range(self.POLE_MIN_BARS, self.POLE_MAX_BARS + 1):
            pole_start = pole_high_idx - pole_len + 1
            if pole_start < 0:
                break
            start_open = float(ind.open.iloc[pole_start])
            if start_open <= 0:
                continue
            gain = (end_close - start_open) / start_open
            if gain < self.POLE_GAIN_MIN:
                continue

            # F2: pole avg volume vs the 20-bar baseline just before the pole.
            base_lo = pole_start - self.VOL_BASELINE_BARS
            if base_lo < 0:
                continue
            baseline = float(ind.volume.iloc[base_lo:pole_start].mean())
            pole_avg_vol = float(ind.volume.iloc[pole_start : pole_high_idx + 1].mean())
            if baseline <= 0:
                continue
            vol_ratio = pole_avg_vol / baseline
            if vol_ratio < self.POLE_VOL_EXPANSION:
                continue

            # C13: pre-existing uptrend at pole start.
            if not self._trend_ok(ind, sma, pole_start):
                continue

            if gain > best_gain:
                best_gain = gain
                best = (pole_start, gain, vol_ratio, pole_avg_vol)

        return best

    def _trend_ok(self, ind: IndicatorEngine, sma: pd.Series, pole_start: int) -> bool:
        """C13: close ≥ 50-SMA at pole start AND the SMA rising vs 5 bars back."""
        prev = pole_start - self.TREND_SMA_LOOKBACK
        if prev < 0:
            return False
        sma_now  = float(sma.iloc[pole_start])
        sma_prev = float(sma.iloc[prev])
        if pd.isna(sma_now) or pd.isna(sma_prev):
            return False
        close_start = float(ind.close.iloc[pole_start])
        return close_start >= sma_now and sma_now > sma_prev

    def _is_first_breakout(
        self, ind: IndicatorEngine, flag_end: int, cur: int, flag_high: float
    ) -> bool:
        """F7: `cur` is the first close above `flag_high` after the flag ends."""
        for k in range(flag_end + 1, cur):
            if float(ind.close.iloc[k]) > flag_high:
                return False  # an earlier bar already broke out
        return float(ind.close.iloc[cur]) > flag_high

    # ── Confidence ─────────────────────────────────────────────────────────────
    def _score_confidence(self, setup: _FlagSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.pole_gain_pct >= 0.40:
            score += 0.10          # a genuinely violent pole
        if setup.pole_vol_ratio >= 1.50:
            score += 0.10          # heavy accumulation on the pole
        if setup.flag_vol_ratio <= 0.60:
            score += 0.10          # decisive volume dry-up in the flag
        if 0.10 <= setup.flag_low_depth_pct <= 0.20:
            score += 0.10          # tight, high-and-tight retracement
        if setup.flag_drift_pct <= 0.0:
            score += 0.05          # flag actually pulled back, not drifted up

        return min(score, 1.0)
