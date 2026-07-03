"""
patterns/pattern_007_descending_channel.py — Descending Channel breakout LONG setup.

Inverse of patterns/pattern_006_upward_channel.py. A falling parallel channel
defined by two swing lows (SL1, SL2) and the peak between them. Price makes a
lower low at SL2 on higher RSI (bullish divergence), then breaks the falling
upper channel line. We enter LONG on the second consecutive close above that
line.

Structure (C1 – C12), inverse of the upward channel:
  C1  Channel start → SL1 downtrend ≥ 15%. Channel start = highest high in
      the 200 bars before SL1.
  C2  Peak high < channel-start high (descending ceiling).
  C3  Floor intact: no bar between SL1 and SL2 closes below SL2 low.
  C4  SL1 RSI ≤ 45 (genuine oversold trough).
  C5  SL2 low ≤ SL1 low × 0.98 (lower low).
  C6  SL2 RSI > SL1 RSI (bullish divergence).
  C7  RSI divergence gap ≥ 5 points.
  C8  SL2 RSI in 25–65.
  C9  ≥ 20 bars between SL1 and SL2.
  C10 ≤ 180 bars between SL1 and SL2.
  C11 Peak height ≥ 2% above SL1 low.
  C12 Peak height ≤ 25% above SL1 low.

Entry (C13 – C15 + dual RSI):
  C13 2 consecutive closes above the falling upper channel line.
       upper_line(k) = peak_high + slope × (k − peak_idx),
       slope = (SL2 low − SL1 low) / (i2 − i1)  (negative — channel falls).
  C14 Cancel if any close falls below SL2 low before the break is confirmed.
  C15 Entry at the close of the 2nd confirming bar (LONG).
  v7+  RSI at the break bar > SL2 RSI (divergence still rising at entry).

Trade management (C16 – C20):
  C16 Hard stop at SL2 low × 0.99.
  C17 Measured-move target = entry + channel width (channel height projected
      above the break).
  C18 7% gain cap from entry — exit at whichever of C17 / C18 is closer.
  C19 Time stop: exit at the close of bar 15 if nothing else triggered.
  C20 Trailing stop activates after 4% gain; trails 2.5% below the best
      (highest) close since entry.

v9 filter:
  Skip the trade if any SEC EDGAR 8-K item 2.02 earnings filing date falls
  within [entry bar, bar 15]. See data/edgar_client.py.

Notes on backtester wiring:
  - The 15-bar time stop (C19) is delivered via the existing
    `exit_bars_after_neckline_break` mechanism: `neckline` is set to the
    upper-channel-line value AT the entry bar (which the entry close is
    above by construction), so the entry bar itself is recorded as the
    "neckline break" bar and the time exit fires 15 bars later.
  - The trailing stop's 4% activation threshold (C20) is NOT enforced by
    the current backtester, which applies the trailing stop from bar 1.
    `trailing_stop_pct` is still set (2.5%, highest_close) so the trail is
    correct once price has moved in our favour; the activation gate is a
    known TODO on the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from patterns.base_pattern import (
    BasePattern, TradeSignal,
    ann_marker, ann_hline, ann_segment,
    ANN_PEAK, ANN_TROUGH, ANN_LINE, ANN_STOP, ANN_TARGET, ANN_ENTRY, ANN_REF,
)
from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from data.edgar_client import default_client as edgar_client
from analysis.indicator_engine import IndicatorEngine
from utils.logger import log


@dataclass(frozen=True)
class _DescendingChannelSetup:
    sl1_idx: int
    sl2_idx: int
    peak_idx: int
    channel_start_idx: int
    sl1_low: float
    sl2_low: float
    peak_high: float
    channel_start_high: float
    slope: float              # price per bar (fall of the parallel channel, < 0)
    channel_width: float      # vertical distance between the two lines
    sl1_rsi: float
    sl2_rsi: float
    break_rsi: float
    rsi_divergence: float
    peak_height_pct: float
    downtrend_pct: float
    entry_idx: int
    entry_upper_line: float   # upper-channel-line value at the entry bar


class DescendingChannelPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_007_descending_channel"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A falling descending channel on a daily chart: two lower swing "
            "lows (SL1, SL2) with a lower peak ceiling between them, forming "
            "two parallel downward-sloping lines. SL2 makes a lower low than "
            "SL1 but on higher RSI (bullish divergence). Entry is a LONG on "
            "the close of the second consecutive bar that closes above the "
            "falling upper channel line."
        )

    # ── Parameters (inverse of pattern_006) ────────────────────────────────────
    RSI_PERIOD              = 14
    DOWNTREND_MIN           = 0.15        # C1
    CHANNEL_START_LOOKBACK  = 200         # C1: highest high in 200 bars before SL1
    SL1_RSI_MAX             = 45.0        # C4
    SL2_SL1_RATIO_MAX       = 0.98        # C5
    RSI_DIV_GAP_MIN         = 5.0         # C7
    SL2_RSI_MIN             = 25.0        # C8 (mirror of 35–75 → 25–65)
    SL2_RSI_MAX             = 65.0        # C8
    SL_GAP_MIN              = 20          # C9
    SL_GAP_MAX              = 180         # C10
    PEAK_HEIGHT_MIN         = 0.02        # C11
    PEAK_HEIGHT_MAX         = 0.25        # C12
    STOP_BELOW_SL2          = 0.99        # C16
    GAIN_CAP_PCT            = 0.07        # C18
    TIME_STOP_BARS          = 15          # C19
    TRAIL_ACTIVATION_PCT    = 0.04        # C20 (activation; not enforced by backtester)
    TRAILING_STOP_PCT       = 0.025       # C20
    SWING_LOOKBACK          = 2
    MIN_BARS                = 210
    POSITION_NOTIONAL       = 10_000.0
    V9_EARNINGS_BLACKOUT    = True        # v9: skip trade on EDGAR 8-K 2.02 window

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
        if len(swing_lows) < 2:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for sl2_idx in reversed(swing_lows):
            if sl2_idx + 2 > cur:
                continue  # need at least 2 bars after SL2 to confirm a break

            sl1_candidates = [i for i in swing_lows if i < sl2_idx]
            for sl1_idx in reversed(sl1_candidates):
                setup = self._evaluate_channel(ind, rsi, sl1_idx, sl2_idx, cur)
                if setup is None:
                    continue
                if setup.entry_idx != cur:
                    continue  # only fire on the exact break bar

                # v9: earnings blackout over [entry, entry + TIME_STOP_BARS].
                if self.V9_EARNINGS_BLACKOUT and self._in_earnings_blackout(
                    df, symbol, setup.entry_idx
                ):
                    log.info(
                        f"[{self.name}] {symbol} {timeframe} | "
                        f"v9 blackout: 8-K 2.02 in "
                        f"[{df.index[setup.entry_idx].date()}, +{self.TIME_STOP_BARS}b] "
                        f"— skipping LONG"
                    )
                    return None

                confidence = self._score_confidence(setup)
                close = float(ind.close.iloc[cur])
                qty = round(self.POSITION_NOTIONAL / close, 4)

                stop = round(setup.sl2_low * self.STOP_BELOW_SL2, 4)
                target_measured = close + setup.channel_width
                target_cap = close * (1 + self.GAIN_CAP_PCT)
                # For a long the closer target is the LOWER of the two.
                take_profit = round(min(target_measured, target_cap), 4)

                # Lower channel line endpoint at entry for the overlay.
                lower_at_entry = setup.sl1_low + setup.slope * (setup.entry_idx - sl1_idx)

                log.info(
                    f"[{self.name}] {symbol} {timeframe} | "
                    f"LONG entry | SL1@{sl1_idx} SL2@{sl2_idx} "
                    f"peak@{setup.peak_idx} "
                    f"downtrend={setup.downtrend_pct:.1%} "
                    f"height={setup.peak_height_pct:.1%} "
                    f"width={setup.channel_width:.2f} "
                    f"RSI_div={setup.rsi_divergence:.1f} "
                    f"break_RSI={setup.break_rsi:.1f} "
                    f"stop={stop:.2f} tp={take_profit:.2f} "
                    f"confidence={confidence:.2f}"
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
                    neckline=round(setup.entry_upper_line, 4),
                    neckline_break_direction="above",
                    exit_bars_after_neckline_break=self.TIME_STOP_BARS,
                    notes=(
                        f"Descending channel | SL1@{sl1_idx} SL2@{sl2_idx} "
                        f"peak@{setup.peak_idx} | "
                        f"width={setup.channel_width:.2f} "
                        f"RSI_div={setup.rsi_divergence:.1f} "
                        f"break_RSI={setup.break_rsi:.1f} | "
                        f"stop={stop:.2f} tp={take_profit:.2f}"
                    ),
                    chart_annotations=[
                        ann_marker(self.bar_date(df, setup.channel_start_idx), setup.channel_start_high, "start", ANN_REF, "v", "above"),
                        ann_marker(self.bar_date(df, sl1_idx), setup.sl1_low, "SL1", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, sl2_idx), setup.sl2_low, "SL2", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, setup.peak_idx), setup.peak_high, "peak", ANN_PEAK, "v", "above"),
                        ann_segment(self.bar_date(df, setup.peak_idx), self.bar_date(df, setup.entry_idx),
                                    setup.peak_high, setup.entry_upper_line, ANN_LINE, "-", 1.4),
                        ann_segment(self.bar_date(df, sl1_idx), self.bar_date(df, setup.entry_idx),
                                    setup.sl1_low, lower_at_entry, ANN_LINE, "-", 1.4),
                        ann_hline(stop, "stop", ANN_STOP),
                        ann_hline(take_profit, "TP", ANN_TARGET),
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

    def _evaluate_channel(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        sl1_idx: int,
        sl2_idx: int,
        cur: int,
    ) -> _DescendingChannelSetup | None:
        gap = sl2_idx - sl1_idx
        if gap < self.SL_GAP_MIN or gap > self.SL_GAP_MAX:
            return None  # C9 / C10

        sl1_low = float(ind.low.iloc[sl1_idx])
        sl2_low = float(ind.low.iloc[sl2_idx])
        sl1_rsi = float(rsi.iloc[sl1_idx])
        sl2_rsi = float(rsi.iloc[sl2_idx])

        # C5: SL2 lower low.
        if sl2_low > sl1_low * self.SL2_SL1_RATIO_MAX:
            return None
        # C4: SL1 oversold.
        if sl1_rsi > self.SL1_RSI_MAX:
            return None
        # C8: SL2 RSI band.
        if sl2_rsi < self.SL2_RSI_MIN or sl2_rsi > self.SL2_RSI_MAX:
            return None
        # C6 + C7: bullish divergence with a ≥5pt gap.
        if sl2_rsi <= sl1_rsi:
            return None
        rsi_div = sl2_rsi - sl1_rsi
        if rsi_div < self.RSI_DIV_GAP_MIN:
            return None

        # C1: channel start = highest high in the 200 bars before SL1.
        start_lo = max(0, sl1_idx - self.CHANNEL_START_LOOKBACK)
        if sl1_idx - start_lo < 2:
            return None
        start_slice = ind.high.iloc[start_lo:sl1_idx]
        channel_start_idx = start_lo + int(start_slice.values.argmax())
        channel_start_high = float(ind.high.iloc[channel_start_idx])
        if channel_start_high <= 0:
            return None
        downtrend = (channel_start_high - sl1_low) / channel_start_high
        if downtrend < self.DOWNTREND_MIN:
            return None

        # Peak = highest high strictly between SL1 and SL2.
        peak_slice = ind.high.iloc[sl1_idx + 1 : sl2_idx]
        if peak_slice.empty:
            return None
        peak_idx = sl1_idx + 1 + int(peak_slice.values.argmax())
        peak_high = float(ind.high.iloc[peak_idx])

        # C2: descending ceiling.
        if peak_high >= channel_start_high:
            return None

        # C11 / C12: peak height above SL1 low.
        if sl1_low <= 0:
            return None
        peak_height = (peak_high - sl1_low) / sl1_low
        if peak_height < self.PEAK_HEIGHT_MIN:
            return None
        if peak_height > self.PEAK_HEIGHT_MAX:
            return None

        # C3: floor intact — no bar between SL1 and SL2 closes below SL2 low.
        between_close = ind.close.iloc[sl1_idx + 1 : sl2_idx]
        if not between_close.empty and float(between_close.min()) < sl2_low:
            return None

        # Channel geometry: parallel falling lines through (SL1,SL2) and peak.
        slope = (sl2_low - sl1_low) / (sl2_idx - sl1_idx)  # negative
        # Lower line at peak_idx gives the channel height (constant width).
        lower_at_peak = sl1_low + slope * (peak_idx - sl1_idx)
        channel_width = peak_high - lower_at_peak
        if channel_width <= 0:
            return None

        def upper_line(k: int) -> float:
            return peak_high + slope * (k - peak_idx)

        # C13 / C14 / C15 + v7: scan for the first 2-consec close above the
        # falling upper line, cancelling if any close first prints below SL2.
        entry_idx = self._find_break(
            ind, rsi, sl2_idx, sl2_low, upper_line, cur
        )
        if entry_idx is None:
            return None

        break_rsi = float(rsi.iloc[entry_idx])
        # v7+: divergence still rising at the break bar.
        if break_rsi <= sl2_rsi:
            return None

        return _DescendingChannelSetup(
            sl1_idx=sl1_idx,
            sl2_idx=sl2_idx,
            peak_idx=peak_idx,
            channel_start_idx=channel_start_idx,
            sl1_low=sl1_low,
            sl2_low=sl2_low,
            peak_high=peak_high,
            channel_start_high=channel_start_high,
            slope=float(slope),
            channel_width=float(channel_width),
            sl1_rsi=sl1_rsi,
            sl2_rsi=sl2_rsi,
            break_rsi=break_rsi,
            rsi_divergence=rsi_div,
            peak_height_pct=peak_height,
            downtrend_pct=downtrend,
            entry_idx=entry_idx,
            entry_upper_line=upper_line(entry_idx),
        )

    def _find_break(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        sl2_idx: int,
        sl2_low: float,
        upper_line,
        cur: int,
    ) -> int | None:
        """First bar k where close[k-1] and close[k] both close above the
        falling upper line. Returns None if C14 cancels or no break yet.

        C14: cancel if any close between SL2 and the break falls below SL2 low.
        """
        consec = 0
        for k in range(sl2_idx + 1, cur + 1):
            close_k = float(ind.close.iloc[k])
            # C14: floor breach before a confirmed break cancels the setup.
            if close_k < sl2_low:
                return None
            if close_k > upper_line(k):
                consec += 1
            else:
                consec = 0
            if consec >= 2:
                return k
        return None

    # ── v9 earnings blackout ───────────────────────────────────────────────────
    def _in_earnings_blackout(
        self, df: pd.DataFrame, symbol: str, entry_idx: int
    ) -> bool:
        end_idx = min(entry_idx + self.TIME_STOP_BARS, len(df) - 1)
        try:
            start_d = _to_date(df.index[entry_idx])
            end_d = _to_date(df.index[end_idx])
        except (AttributeError, ValueError):
            return False
        try:
            return edgar_client().has_earnings_in(symbol, start_d, end_d)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"[{self.name}] {symbol}: v9 EDGAR check error ({exc!r}); "
                f"treating as no-blackout"
            )
            return False

    # ── Confidence ─────────────────────────────────────────────────────────────
    def _score_confidence(self, setup: _DescendingChannelSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.downtrend_pct >= 0.25:
            score += 0.10
        if setup.rsi_divergence >= 10.0:
            score += 0.10
        if setup.sl2_rsi >= 40.0:
            score += 0.10
        gap = setup.sl2_idx - setup.sl1_idx
        if 30 <= gap <= 120:
            score += 0.10
        if 0.05 <= setup.peak_height_pct <= 0.15:
            score += 0.05  # clean, moderate rally

        return min(score, 1.0)


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).split(" ")[0], "%Y-%m-%d").date()
