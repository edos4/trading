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
    ann_segment,
)


@dataclass(frozen=True)
class _Setup:
    left_shoulder: int
    left_neck: int
    head: int
    right_neck: int
    right_shoulder: int
    entry: int
    neckline: float
    target: float


class HeadAndShouldersPattern(BasePattern):
    MIN_BARS = 80
    HORIZON_BARS = 11
    POSITION_NOTIONAL = 10_000.0

    @property
    def name(self) -> str:
        return "pattern_008_head_and_shoulders"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    @property
    def chart_description(self) -> str:
        return "Head and shoulders top with a strict central head, lower right shoulder, two neckline troughs, bearish RSI divergence, and a neckline-or-day-seven short entry."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None:
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi_wilder(14)
        current = _dedup.current_bar(len(df) - 1)
        n = len(df)
        head_max = n - 3 - self.SWING_LB - 10  # `.cjs` hdIdx < n - MIN_RS - LB - 10
        for head in extrema(ind.close, "high", self.SWING_LB, strict=True):
            if head > head_max or _dedup.used(head):
                continue
            setup = self._evaluate(ind, rsi, head, current)
            if setup is None or setup.entry != current:
                continue
            if _dedup.used(setup.right_shoulder):
                continue
            price = float(ind.close.iloc[current])
            rs_close = float(ind.close.iloc[setup.right_shoulder])
            return TradeSignal(
                symbol=snapshot.symbol,
                action="SELL",
                pattern=self.name,
                timeframe=snapshot.timeframe,
                confidence=1.0,
                price=price,
                qty=self.POSITION_NOTIONAL / price if price > 0 else 0.0,
                # .cjs backtest_hs_200: a close back above the right shoulder
                # invalidates the short.
                stop_loss=round(rs_close, 4),
                stop_loss_on_close=True,
                setup_key=(setup.head, setup.right_shoulder),
                take_profit=round(setup.target, 4),
                trailing_stop_pct=0.03,
                trailing_stop_mode="lowest_close",
                trailing_activation_pct=0.0,
                trailing_stop_on_close=True,
                exit_bars_after_entry=10,
                notes=f"Head and shoulders LS={setup.left_shoulder} H={setup.head} RS={setup.right_shoulder} neckline={setup.neckline:.2f}",
                chart_annotations=[
                    ann_marker(self.bar_date(df, setup.left_shoulder), float(ind.close.iloc[setup.left_shoulder]), "LS", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.left_neck), float(ind.close.iloc[setup.left_neck]), "LN", ANN_TROUGH, "^", "below"),
                    ann_marker(self.bar_date(df, setup.head), float(ind.close.iloc[setup.head]), "HEAD", ANN_PEAK, "v", "above"),
                    ann_marker(self.bar_date(df, setup.right_neck), float(ind.close.iloc[setup.right_neck]), "RN", ANN_TROUGH, "^", "below"),
                    ann_marker(self.bar_date(df, setup.right_shoulder), float(ind.close.iloc[setup.right_shoulder]), "RS", ANN_PEAK, "v", "above"),
                    ann_segment(self.bar_date(df, setup.left_neck), self.bar_date(df, setup.entry), float(ind.close.iloc[setup.left_neck]), setup.neckline, ANN_LINE),
                    ann_hline(setup.target, "target", ANN_TARGET),
                    ann_marker(self.bar_date(df, setup.entry), price, "entry", ANN_ENTRY, "o", "above"),
                ],
            )
        return None

    SWING_LB = 4
    MIN_LS = 10
    MAX_LS = 80
    RN_WINDOW = 60
    RS_AFTER_RN_MIN = 3
    RS_AFTER_RN_MAX = 50
    OUTCOME_WINDOW = 40

    def _evaluate(self, ind: IndicatorEngine, rsi, head: int, current: int) -> _Setup | None:
        n = len(ind.close)
        maxima = extrema(ind.close, "high", self.SWING_LB, strict=True)
        head_close = float(ind.close.iloc[head])
        head_rsi = float(rsi.iloc[head])
        if not np.isfinite(head_rsi):
            return None

        # left shoulder: highest local-max close in [head-80, head-10] below the head
        ls_candidates = [
            i for i in maxima
            if max(0, head - self.MAX_LS) <= i <= head - self.MIN_LS
            and float(ind.close.iloc[i]) < head_close
        ]
        if not ls_candidates:
            return None
        left_shoulder = max(ls_candidates, key=lambda i: float(ind.close.iloc[i]))
        ls_close = float(ind.close.iloc[left_shoulder])
        ls_rsi = float(rsi.iloc[left_shoulder])
        if not np.isfinite(ls_rsi) or ls_rsi - head_rsi < 2.0:
            return None

        left_slice = ind.close.iloc[left_shoulder + 1 : head].to_numpy(dtype=float)
        if not left_slice.size:
            return None
        left_neck = left_shoulder + 1 + int(np.argmin(left_slice))
        ln_close = float(ind.close.iloc[left_neck])
        if (ls_close - ln_close) / ls_close < 0.05:
            return None

        # right neck: deepest close in a fixed [head+1, head+60] window
        rn_end = min(head + self.RN_WINDOW, n - self.SWING_LB - 8)
        if rn_end <= head + 1:
            return None
        rn_slice = ind.close.iloc[head + 1 : rn_end + 1].to_numpy(dtype=float)
        right_neck = head + 1 + int(np.argmin(rn_slice))
        rn_close = float(ind.close.iloc[right_neck])
        if (head_close - rn_close) / head_close < 0.05:
            return None

        # flat neckline = average of the two neck closes
        neckline = (ln_close + rn_close) / 2.0
        if neckline <= 0:
            return None
        skew = (rn_close - ln_close) / neckline
        if skew > 0.10 or abs(skew) > 0.30:
            return None
        if (head_close - neckline) / neckline < 0.10:
            return None

        # right shoulder: highest local-max close in [rn+3, rn+50] above the
        # neckline (by >= 5%) and below the head
        rs_lo = right_neck + self.RS_AFTER_RN_MIN
        rs_hi = min(right_neck + self.RS_AFTER_RN_MAX, n - self.SWING_LB - 3)
        rs_cands = [
            i for i in maxima
            if rs_lo <= i <= rs_hi
            and float(ind.close.iloc[i]) < head_close
            and float(ind.close.iloc[i]) > neckline
            and (float(ind.close.iloc[i]) - neckline) / neckline >= 0.05
        ]
        if not rs_cands:
            return None
        right_shoulder = max(rs_cands, key=lambda i: float(ind.close.iloc[i]))
        rs_close = float(ind.close.iloc[right_shoulder])
        rs_rsi = float(rsi.iloc[right_shoulder])
        if not np.isfinite(rs_rsi):
            return None
        if rs_close >= ls_close or rs_rsi >= head_rsi or rs_rsi > 60.0:
            return None

        p_bars = right_shoulder - left_shoulder
        if p_bars < 20 or p_bars > 120:
            return None
        if right_shoulder - head > (head - left_shoulder) * 2.5:
            return None
        if right_shoulder + 2 >= n:
            return None
        if (float(ind.close.iloc[right_shoulder + 1]) >= rs_close
                or float(ind.close.iloc[right_shoulder + 2]) >= rs_close):
            return None

        # C14: 2nd-consecutive-close below neckline (else day-7, only if there
        # was at least one break). Scan is capped at RS + OUTCOME_WINDOW.
        scan_end = min(right_shoulder + self.OUTCOME_WINDOW, n - 1)
        first_break = consec_break = None
        for k in range(right_shoulder + 1, scan_end + 1):
            if float(ind.close.iloc[k]) > head_close:
                break
            if float(ind.close.iloc[k]) < neckline:
                if first_break is None:
                    first_break = k
                if k + 1 < n and float(ind.close.iloc[k + 1]) < neckline:
                    consec_break = k + 1
                    break
        day7 = right_shoulder + 7
        if consec_break is not None:
            entry = min(day7, consec_break)
        elif first_break is not None and day7 < n:
            entry = day7
        else:
            return None
        if entry != current:
            return None
        target = neckline - (head_close - neckline)
        if target <= 0:
            return None
        return _Setup(left_shoulder, left_neck, head, right_neck, right_shoulder,
                      entry, neckline, target)
