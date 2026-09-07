from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns import _dedup
from patterns._rules import extrema
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_LINE,
    ANN_PEAK,
    ANN_TARGET,
    ANN_TROUGH,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
)


@dataclass(frozen=True)
class _Setup:
    h1: int
    h2: int
    valley: int
    neckline: float
    h1_high: float
    h2_high: float
    h1_rsi: float
    h2_rsi: float
    entry: int
    neckline_break_bar: int


class DoubleTopPattern(BasePattern):
    """`.cjs` backtest_doubletop.cjs (C1-C13 detection) + backtest_14b.cjs
    (C14 entry / C15 exit). Two swing highs, lower + weaker second peak,
    5% valley, weak recovery volume, neckline break within 30 bars; short on
    day-7 or the break (whichever first); exit on the 3% intraday trailing
    stop, the 10%-below-neckline target, or 5 bars after the break."""

    RSI_PERIOD = 14
    LB = 3                     # `.cjs` isSwingHigh lb
    H1_RSI_MIN = 70.0          # C5
    H2_RSI_MIN = 50.0          # C6
    H2_RSI_MAX = 61.0          # C11
    RSI_DIV_MIN = 3.0          # C8 (strict >)
    VALLEY_DEPTH_MIN = 0.05    # C1
    GAP_MIN = 8               # C3
    GAP_MAX = 90              # C3
    VALLEY_WINDOW = 65        # `.cjs` deepest low in [H1+1, H1+65]
    OUTCOME_WINDOW = 30      # neckline must break within 30 bars of H2
    ENTRY_DELAY = 7          # C14
    TARGET_BELOW_NECKLINE = 0.10   # C14/C15 target = neckline x 0.90
    EXIT_AFTER_NECKLINE_BREAK = 5  # C14
    TRAIL_PCT = 0.03              # C15 (intraday high vs lowest-close x 1.03)
    SCAN_END_BARS = 30           # `.cjs` scanEnd = entry + 30 -> timeout
    MIN_BARS = 30
    HORIZON_BARS = 3  # `.cjs` backtest_14b: a trade can time out with few trailing bars
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_002_double_top"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Double top with a lower, weaker second high, bearish RSI divergence, a five-percent valley, weak recovery volume, and a day-seven-or-neckline-break short entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi_wilder(self.RSI_PERIOD)
        current = _dedup.current_bar(len(df) - 1)
        n = len(df)
        highs = extrema(ind.high, "high", self.LB, strict=True)

        for h1 in highs:
            if _dedup.used(h1) or h1 + self.GAP_MIN + self.OUTCOME_WINDOW + 3 >= n:
                continue
            h1_rsi = float(rsi.iloc[h1])
            if not np.isfinite(h1_rsi) or h1_rsi < self.H1_RSI_MIN:
                continue
            h1_high = float(ind.high.iloc[h1])
            # C1: deepest valley in the H1+1 .. H1+65 window
            v_end = min(h1 + self.VALLEY_WINDOW, n - self.OUTCOME_WINDOW - 3)
            if v_end <= h1 + 1:
                continue
            vslice = ind.low.iloc[h1 + 1 : v_end + 1].to_numpy(dtype=float)
            valley = h1 + 1 + int(np.argmin(vslice))
            neckline = float(ind.low.iloc[valley])
            if (h1_high - neckline) / h1_high < self.VALLEY_DEPTH_MIN:
                continue

            # `.cjs` scanDoubleTop caps the H2 search at n - OUTCOME_WINDOW - 3
            # and *commits to the first structurally valid H2*: if that H2's
            # outcome window shows no neckline break it is still consumed (the
            # `usedH1` + `break`), so a later, deeper H2 is never considered.
            # Only a C13 cancellation ("continue") lets the search move on.
            h2_cap = n - self.OUTCOME_WINDOW - 3
            for h2 in highs:
                if h2 < valley + 3:
                    continue
                if h2 - h1 < self.GAP_MIN:
                    continue
                if h2 - h1 > self.GAP_MAX or h2 > h2_cap:
                    break
                verdict = self._evaluate(ind, rsi, h1, h2, valley, neckline,
                                         h1_high, h1_rsi, n)
                if verdict is None:                 # structural gate failed
                    continue
                if verdict == "cancelled":          # C13 — keep searching
                    continue
                if verdict == "pending":            # committed, no break -> no trade
                    _dedup.mark(h1)
                    break
                setup = verdict
                if setup.entry != current:
                    break                           # committed; fires on its own bar
                price = float(ind.close.iloc[current])
                target = round(neckline * (1 - self.TARGET_BELOW_NECKLINE), 4)
                return TradeSignal(
                    symbol=snapshot.symbol,
                    action="SELL",
                    pattern=self.name,
                    timeframe=snapshot.timeframe,
                    confidence=1.0,
                    price=price,
                    qty=self.POSITION_NOTIONAL / price if price > 0 else 0.0,
                    setup_key=(h1, h2),
                    exit_order="trail_first",
                    take_profit=target,
                    trailing_stop_pct=self.TRAIL_PCT,
                    trailing_stop_mode="lowest_close",
                    trailing_activation_pct=0.0,
                    neckline=neckline,
                    neckline_break_direction="below",
                    exit_bars_after_neckline_break=self.EXIT_AFTER_NECKLINE_BREAK,
                    exit_bars_after_entry=self.SCAN_END_BARS,
                    notes=f"Double top H1={h1} H2={h2} neckline={neckline:.2f} "
                          f"RSI={setup.h1_rsi:.1f}->{setup.h2_rsi:.1f} "
                          f"break@{setup.neckline_break_bar}",
                    chart_annotations=[
                        ann_marker(self.bar_date(df, h1), h1_high, "H1", ANN_PEAK, "v", "above"),
                        ann_marker(self.bar_date(df, valley), neckline, "valley", ANN_TROUGH, "^", "below"),
                        ann_marker(self.bar_date(df, h2), setup.h2_high, "H2", ANN_PEAK, "v", "above"),
                        ann_hline(neckline, "neckline", ANN_LINE),
                        ann_hline(target, "target", ANN_TARGET),
                        ann_marker(self.bar_date(df, current), price, "entry", ANN_ENTRY, "o", "above"),
                    ],
                )
        return None

    def _evaluate(self, ind, rsi, h1, h2, valley, neckline, h1_high, h1_rsi,
                  n) -> _Setup | str | None:
        """`.cjs` scanDoubleTop verdict for one (H1, H2) pair:
        ``None`` — a structural gate (C2/C4/C6-C12) failed, try the next H2;
        ``"cancelled"`` — C13: a post-H2 bar exceeded H2 before any neckline
        break, try the next H2;
        ``"pending"`` — structure holds but the neckline never broke in the
        outcome window: `.cjs` still consumes H1 here, so no trade;
        ``_Setup`` — confirmed break, carries the entry bar."""
        h2_high = float(ind.high.iloc[h2])
        h2_close = float(ind.close.iloc[h2])
        h1_close = float(ind.close.iloc[h1])
        h2_rsi = float(rsi.iloc[h2])
        if not np.isfinite(h2_rsi):
            return None
        if h2_high >= h1_high:                       # C4
            return None
        if h2_close >= h1_close:                     # C12
            return None
        if h2_rsi >= h1_rsi:                         # C2
            return None
        if h2_rsi < self.H2_RSI_MIN or h2_rsi > self.H2_RSI_MAX:   # C6 / C11
            return None
        if h1_rsi - h2_rsi <= self.RSI_DIV_MIN:      # C8
            return None
        # C7: pattern intact between H1 and H2
        between = ind.high.iloc[h1 + 1 : h2].to_numpy(dtype=float)
        if between.size and float(between.max()) > h1_high:
            return None
        # C10: leg-2 recovery volume weak (avg up-bar vol < avg down-bar vol)
        o = ind.open.iloc[valley + 1 : h2 + 1].to_numpy(dtype=float)
        c = ind.close.iloc[valley + 1 : h2 + 1].to_numpy(dtype=float)
        v = ind.volume.iloc[valley + 1 : h2 + 1].to_numpy(dtype=float)
        up = v[c >= o]
        down = v[c < o]
        if up.size and down.size and up.mean() >= down.mean():
            return None
        # C13 + outcome: neckline must break within 30 bars of H2, and no bar
        # may exceed H2's high before that.
        days_to_cross = None
        scan_end = min(h2 + self.OUTCOME_WINDOW, n - 1)
        for k in range(h2 + 1, scan_end + 1):
            if float(ind.close.iloc[k]) < neckline:
                days_to_cross = k - h2
                break
            if float(ind.high.iloc[k]) > h2_high:
                return "cancelled"  # C13
        if days_to_cross is None:
            return "pending"
        entry = h2 + min(self.ENTRY_DELAY, days_to_cross)
        return _Setup(h1, h2, valley, neckline, h1_high, h2_high, h1_rsi, h2_rsi,
                      entry, h2 + days_to_cross)
