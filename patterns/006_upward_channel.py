"""
patterns/pattern_006_upward_channel.py — Upward Channel breakdown short setup.

A rising parallel channel defined by two swing highs (SH1, SH2) and the
valley between them. Price makes a higher high at SH2 on weaker RSI
(bearish divergence), then breaks the rising lower channel line. We enter
SHORT on the second consecutive close below that line.

Structure (C1 – C12):
  C1  Channel start → SH1 uptrend ≥ 15%. Channel start = lowest low in the
      200 bars before SH1.
  C2  Valley low > channel-start low (ascending floor).
  C3  Ceiling intact: no bar between SH1 and SH2 closes above SH2 high.
  C4  SH1 RSI ≥ 55 (genuine overbought peak).
  C5  SH2 high ≥ SH1 high × 1.02 (higher high).
  C6  SH2 RSI < SH1 RSI (bearish divergence).
  C7  RSI divergence gap ≥ 5 points.
  C8  SH2 RSI in 35–75.
  C9  ≥ 20 bars between SH1 and SH2.
  C10 ≤ 180 bars between SH1 and SH2.
  C11 Valley depth ≥ 2% below SH1 high.
  C12 Valley depth ≤ 25% below SH1 high.

Entry (C13 – C15 + dual RSI):
  C13 2 consecutive closes below the rising lower channel line.
       lower_line(k) = valley_low + slope × (k − valley_idx),
       slope = (SH2 high − SH1 high) / (i2 − i1).
  C14 Cancel if any close exceeds SH2 high before the break is confirmed.
  C15 Entry at the close of the 2nd confirming bar (SHORT).
  v7+  RSI at the break bar < SH2 RSI (divergence still declining at entry).

Trade management (C16 – C20, C24):
  C16 Hard stop at SH2 high × 1.01.
  C17 Measured-move target = entry − channel width (channel height projected
      below the break).
  C18 7% gain cap from entry — exit at whichever of C17 / C18 is closer.
  C19 Time stop: exit at the close of bar 15 if nothing else triggered.
  C20 Trailing stop activates after 4% gain; trails 2.5% above the best
      (lowest) close since entry.
  C24 Dual stop (v14): exit at min(SH2 × 1.01, entry × 1.05) — whichever is
      hit first. The fixed entry × 1.05 leg caps damage from violent overnight
      gap-ups the dynamic SH2 stop can't react to (added after WDC gapped 26.8%
      through the SH2 stop on a sector-wide AI-memory rally).

Entry filters (C22 – C23, v12): skip the trade entirely if either fails —
  C22 Freshness: bars from SH2 to the channel-break bar must be ≤ 20. Momentum
      from the divergence peak has a shelf life; slow drifts to the break fail
      more often.
  C23 Don't-chase: the drop already realised from SH2 high to the entry price
      must be ≤ 15%. Entries after a >15% slide are chasing a crowded short.

v9 filter:
  Skip the trade if any SEC EDGAR 8-K item 2.02 earnings filing date falls
  within [entry bar, bar 15]. See data/edgar_client.py.

Not enforced here (C21 reclaim exit, v10): after entry, exit on the first close
  back above the rising lower channel line that also prints a higher high AND
  higher low vs. the prior bar. This is a path-dependent, per-bar exit that the
  current TradeSignal exit API (static stop / target / trailing / neckline time
  stop) can't express, so it lives in the standalone backtest_uc_v14.cjs script
  rather than in this pattern. Documented here so the divergence from the locked
  ruleset is explicit.

Notes on backtester wiring:
  - The 15-bar time stop (C19) is delivered via the existing
    `exit_bars_after_neckline_break` mechanism: `neckline` is set to the
    lower-channel-line value AT the entry bar (which the entry close is
    below by construction), so the entry bar itself is recorded as the
    "neckline break" bar and the time exit fires 15 bars later.
  - The trailing stop's 4% activation threshold (C20) is now enforced by
    the backtester via `trailing_activation_pct`. The trail only activates
    after the entry-to-extreme P&L reaches 4%.
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
class _UpwardChannelSetup:
    sh1_idx: int
    sh2_idx: int
    valley_idx: int
    channel_start_idx: int
    sh1_high: float
    sh2_high: float
    valley_low: float
    channel_start_low: float
    slope: float              # price per bar (rise of the parallel channel)
    channel_width: float      # vertical distance between the two lines
    sh1_rsi: float
    sh2_rsi: float
    break_rsi: float
    rsi_divergence: float
    valley_depth_pct: float
    uptrend_pct: float
    entry_idx: int
    entry_lower_line: float   # lower-channel-line value at the entry bar


class UpwardChannelPattern(BasePattern):

    # ── Identity ───────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "pattern_006_upward_channel"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return (
            "A rising upward channel on a daily chart: two higher swing highs "
            "(SH1, SH2) with a higher valley floor between them, forming two "
            "parallel upward-sloping lines. SH2 makes a higher high than SH1 "
            "but on lower RSI (bearish divergence). Entry is a SHORT on the "
            "close of the second consecutive bar that closes below the rising "
            "lower channel line."
        )

    # ── Parameters (from the locked ruleset) ───────────────────────────────────
    RSI_PERIOD              = 14
    UPTREND_MIN             = 0.15        # C1
    CHANNEL_START_LOOKBACK  = 200         # C1: lowest low in 200 bars before SH1
    SH1_RSI_MIN             = 55.0        # C4
    SH2_SH1_RATIO_MIN       = 1.02        # C5
    RSI_DIV_GAP_MIN         = 5.0         # C7
    SH2_RSI_MIN             = 35.0        # C8
    SH2_RSI_MAX             = 75.0        # C8
    SH_GAP_MIN              = 20          # C9
    SH_GAP_MAX              = 180         # C10
    VALLEY_DEPTH_MIN        = 0.02        # C11
    VALLEY_DEPTH_MAX        = 0.25        # C12
    STOP_ABOVE_SH2          = 1.01        # C16
    FIXED_STOP_PCT          = 0.05        # C24: fixed entry × 1.05 stop leg
    GAIN_CAP_PCT            = 0.20        # C18 (increased from 7% to 20% to let winners run)
    TIME_STOP_BARS          = 15          # C19
    TRAIL_ACTIVATION_PCT    = 0.04        # C20 (activation; not enforced by backtester)
    TRAILING_STOP_PCT       = 0.025       # C20
    FRESHNESS_MAX_BARS      = 20          # C22: SH2 → break bar must be ≤ 20 bars
    DONT_CHASE_MAX_DROP     = 0.15        # C23: ≤ 15% already dropped from SH2 high
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

        swing_highs = self._find_swing_highs(ind.high)
        if len(swing_highs) < 2:
            return None

        n = len(df)
        cur = n + current_idx  # last bar index

        for sh2_idx in reversed(swing_highs):
            if sh2_idx + 2 > cur:
                continue  # need at least 2 bars after SH2 to confirm a break

            sh1_candidates = [i for i in swing_highs if i < sh2_idx]
            for sh1_idx in reversed(sh1_candidates):
                setup = self._evaluate_channel(ind, rsi, sh1_idx, sh2_idx, cur)
                if setup is None:
                    continue
                if setup.entry_idx != cur:
                    continue  # only fire on the exact break bar

                close = float(ind.close.iloc[cur])

                # C22 freshness: the break must arrive within 20 bars of SH2.
                days_to_break = setup.entry_idx - sh2_idx
                if days_to_break > self.FRESHNESS_MAX_BARS:
                    log.debug(
                        f"[{self.name}] {symbol} {timeframe} | "
                        f"C22 skip: daysToBreak={days_to_break} > "
                        f"{self.FRESHNESS_MAX_BARS} (stale break)"
                    )
                    continue

                # C23 don't-chase: skip if price has already fallen >15% from SH2.
                drop_from_sh2 = (setup.sh2_high - close) / setup.sh2_high
                if drop_from_sh2 > self.DONT_CHASE_MAX_DROP:
                    log.debug(
                        f"[{self.name}] {symbol} {timeframe} | "
                        f"C23 skip: dropFromSH2={drop_from_sh2:.1%} > "
                        f"{self.DONT_CHASE_MAX_DROP:.0%} (chasing)"
                    )
                    continue

                # v9: earnings blackout over [entry, entry + TIME_STOP_BARS].
                if self.V9_EARNINGS_BLACKOUT and self._in_earnings_blackout(
                    df, symbol, setup.entry_idx
                ):
                    log.info(
                        f"[{self.name}] {symbol} {timeframe} | "
                        f"v9 blackout: 8-K 2.02 in "
                        f"[{df.index[setup.entry_idx].date()}, +{self.TIME_STOP_BARS}b] "
                        f"— skipping SHORT"
                    )
                    return None

                confidence = self._score_confidence(setup)
                qty = round(self.POSITION_NOTIONAL / close, 4)

                # C24 dual stop: dynamic SH2 × 1.01 OR fixed entry × 1.05,
                # whichever is hit first (i.e. the lower price for a short).
                stop = round(
                    min(
                        setup.sh2_high * self.STOP_ABOVE_SH2,
                        close * (1 + self.FIXED_STOP_PCT),
                    ),
                    4,
                )
                target_measured = close - setup.channel_width
                target_cap = close * (1 - self.GAIN_CAP_PCT)
                # For a short the closer target is the HIGHER of the two.
                take_profit = round(max(target_measured, target_cap), 4)

                # Channel line endpoints for the overlay.
                upper_at_entry = setup.sh1_high + setup.slope * (setup.entry_idx - sh1_idx)

                log.info(
                    f"[{self.name}] {symbol} {timeframe} | "
                    f"SHORT entry | SH1@{sh1_idx} SH2@{sh2_idx} "
                    f"valley@{setup.valley_idx} "
                    f"uptrend={setup.uptrend_pct:.1%} "
                    f"depth={setup.valley_depth_pct:.1%} "
                    f"width={setup.channel_width:.2f} "
                    f"RSI_div={setup.rsi_divergence:.1f} "
                    f"break_RSI={setup.break_rsi:.1f} "
                    f"fresh={days_to_break}b drop={drop_from_sh2:.1%} "
                    f"stop={stop:.2f} tp={take_profit:.2f} "
                    f"confidence={confidence:.2f}"
                )

                return TradeSignal(
                    symbol=symbol,
                    action="SELL",
                    pattern=self.name,
                    timeframe=timeframe,
                    confidence=confidence,
                    price=close,
                    qty=qty,
                    stop_loss=stop,
                    take_profit=take_profit,
                    trailing_stop_pct=self.TRAILING_STOP_PCT,
                    trailing_stop_mode="lowest_close",
                    trailing_activation_pct=self.TRAIL_ACTIVATION_PCT,
                    neckline=round(setup.entry_lower_line, 4),
                    neckline_break_direction="below",
                    exit_bars_after_neckline_break=self.TIME_STOP_BARS,
                    notes=(
                        f"Upward channel | SH1@{sh1_idx} SH2@{sh2_idx} "
                        f"valley@{setup.valley_idx} | "
                        f"width={setup.channel_width:.2f} "
                        f"RSI_div={setup.rsi_divergence:.1f} "
                        f"break_RSI={setup.break_rsi:.1f} | "
                        f"fresh={days_to_break}b drop={drop_from_sh2:.1%} | "
                        f"stop={stop:.2f} tp={take_profit:.2f}"
                    ),
                    chart_annotations=[
                        ann_marker(self.bar_date(df, setup.channel_start_idx), setup.channel_start_low, "start", ANN_REF, "^", "below"),
                        ann_marker(self.bar_date(df, sh1_idx), setup.sh1_high, "SH1", ANN_PEAK, "v", "above"),
                        ann_marker(self.bar_date(df, sh2_idx), setup.sh2_high, "SH2", ANN_PEAK, "v", "above"),
                        ann_marker(self.bar_date(df, setup.valley_idx), setup.valley_low, "valley", ANN_TROUGH, "^", "below"),
                        ann_segment(self.bar_date(df, sh1_idx), self.bar_date(df, setup.entry_idx),
                                    setup.sh1_high, upper_at_entry, ANN_LINE, "-", 1.4),
                        ann_segment(self.bar_date(df, setup.valley_idx), self.bar_date(df, setup.entry_idx),
                                    setup.valley_low, setup.entry_lower_line, ANN_LINE, "-", 1.4),
                        ann_hline(stop, "stop", ANN_STOP),
                        ann_hline(take_profit, "TP", ANN_TARGET),
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

    def _evaluate_channel(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        sh1_idx: int,
        sh2_idx: int,
        cur: int,
    ) -> _UpwardChannelSetup | None:
        gap = sh2_idx - sh1_idx
        if gap < self.SH_GAP_MIN or gap > self.SH_GAP_MAX:
            return None  # C9 / C10

        sh1_high = float(ind.high.iloc[sh1_idx])
        sh2_high = float(ind.high.iloc[sh2_idx])
        sh1_rsi  = float(rsi.iloc[sh1_idx])
        sh2_rsi  = float(rsi.iloc[sh2_idx])

        # C5: SH2 higher high.
        if sh2_high < sh1_high * self.SH2_SH1_RATIO_MIN:
            return None
        # C4: SH1 overbought.
        if sh1_rsi < self.SH1_RSI_MIN:
            return None
        # C8: SH2 RSI band.
        if sh2_rsi < self.SH2_RSI_MIN or sh2_rsi > self.SH2_RSI_MAX:
            return None
        # C6 + C7: bearish divergence with a ≥5pt gap.
        if sh2_rsi >= sh1_rsi:
            return None
        rsi_div = sh1_rsi - sh2_rsi
        if rsi_div < self.RSI_DIV_GAP_MIN:
            return None

        # C1: channel start = lowest low in the 200 bars before SH1.
        start_lo = max(0, sh1_idx - self.CHANNEL_START_LOOKBACK)
        if sh1_idx - start_lo < 2:
            return None
        start_slice = ind.low.iloc[start_lo:sh1_idx]
        channel_start_idx = start_lo + int(start_slice.values.argmin())
        channel_start_low = float(ind.low.iloc[channel_start_idx])
        if channel_start_low <= 0:
            return None
        uptrend = (sh1_high - channel_start_low) / channel_start_low
        if uptrend < self.UPTREND_MIN:
            return None

        # Valley = lowest low strictly between SH1 and SH2.
        valley_slice = ind.low.iloc[sh1_idx + 1 : sh2_idx]
        if valley_slice.empty:
            return None
        valley_idx = sh1_idx + 1 + int(valley_slice.values.argmin())
        valley_low = float(ind.low.iloc[valley_idx])

        # C2: ascending floor.
        if valley_low <= channel_start_low:
            return None

        # C11 / C12: valley depth below SH1 high.
        valley_depth = (sh1_high - valley_low) / sh1_high
        if valley_depth < self.VALLEY_DEPTH_MIN:
            return None
        if valley_depth > self.VALLEY_DEPTH_MAX:
            return None

        # C3: ceiling intact — no bar between SH1 and SH2 closes above SH2 high.
        between_close = ind.close.iloc[sh1_idx + 1 : sh2_idx]
        if not between_close.empty and float(between_close.max()) > sh2_high:
            return None

        # Channel geometry: parallel rising lines through (SH1,SH2) and valley.
        slope = (sh2_high - sh1_high) / (sh2_idx - sh1_idx)
        # Upper line at valley_idx gives the channel height (constant width).
        upper_at_valley = sh1_high + slope * (valley_idx - sh1_idx)
        channel_width = upper_at_valley - valley_low
        if channel_width <= 0:
            return None

        def lower_line(k: int) -> float:
            return valley_low + slope * (k - valley_idx)

        # C13 / C14 / C15 + v7: scan for the first 2-consec close below the
        # rising lower line, cancelling if any close first prints above SH2.
        entry_idx = self._find_break(
            ind, rsi, sh2_idx, sh2_high, lower_line, cur
        )
        if entry_idx is None:
            return None

        break_rsi = float(rsi.iloc[entry_idx])
        # v7+: divergence still declining at the break bar.
        if break_rsi >= sh2_rsi:
            return None

        return _UpwardChannelSetup(
            sh1_idx=sh1_idx,
            sh2_idx=sh2_idx,
            valley_idx=valley_idx,
            channel_start_idx=channel_start_idx,
            sh1_high=sh1_high,
            sh2_high=sh2_high,
            valley_low=valley_low,
            channel_start_low=channel_start_low,
            slope=float(slope),
            channel_width=float(channel_width),
            sh1_rsi=sh1_rsi,
            sh2_rsi=sh2_rsi,
            break_rsi=break_rsi,
            rsi_divergence=rsi_div,
            valley_depth_pct=valley_depth,
            uptrend_pct=uptrend,
            entry_idx=entry_idx,
            entry_lower_line=lower_line(entry_idx),
        )

    def _find_break(
        self,
        ind: IndicatorEngine,
        rsi: pd.Series,
        sh2_idx: int,
        sh2_high: float,
        lower_line,
        cur: int,
    ) -> int | None:
        """First bar k where close[k-1] and close[k] both close below the
        rising lower line. Returns None if C14 cancels or no break yet.

        C14: cancel if any close between SH2 and the break exceeds SH2 high.
        """
        consec = 0
        for k in range(sh2_idx + 1, cur + 1):
            close_k = float(ind.close.iloc[k])
            # C14: ceiling breach before a confirmed break cancels the setup.
            if close_k > sh2_high:
                return None
            if close_k < lower_line(k):
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
    def _score_confidence(self, setup: _UpwardChannelSetup) -> float:
        score = 0.55  # all hard filters passed

        if setup.uptrend_pct >= 0.25:
            score += 0.10
        if setup.rsi_divergence >= 10.0:
            score += 0.10
        if setup.sh2_rsi <= 60.0:
            score += 0.10
        gap = setup.sh2_idx - setup.sh1_idx
        if 30 <= gap <= 120:
            score += 0.10
        if 0.05 <= setup.valley_depth_pct <= 0.15:
            score += 0.05  # clean, moderate pullback

        return min(score, 1.0)


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).split(" ")[0], "%Y-%m-%d").date()
