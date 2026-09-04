from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
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
    MIN_BARS = 130
    SHARES = 25

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
        current = len(df) - 1
        for head in reversed(extrema(ind.close, "high", 4, strict=True)):
            setup = self._evaluate(ind, rsi, head, current)
            if setup is None or setup.entry != current:
                continue
            price = float(ind.close.iloc[current])
            return TradeSignal(
                symbol=snapshot.symbol,
                action="SELL",
                pattern=self.name,
                timeframe=snapshot.timeframe,
                confidence=1.0,
                price=price,
                qty=self.SHARES,
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

    def _evaluate(self, ind: IndicatorEngine, rsi, head: int, current: int) -> _Setup | None:
        head_close = float(ind.close.iloc[head])
        ls_candidates = [
            idx
            for idx in extrema(ind.close, "high", 4, strict=True)
            if max(4, head - 80) <= idx <= head - 10 and float(ind.close.iloc[idx]) < head_close
        ]
        if not ls_candidates:
            return None
        left_shoulder = max(ls_candidates, key=lambda idx: float(ind.close.iloc[idx]))
        ls_close = float(ind.close.iloc[left_shoulder])
        left_slice = ind.close.iloc[left_shoulder + 1 : head]
        if left_slice.empty:
            return None
        left_neck = left_shoulder + 1 + int(np.argmin(left_slice.to_numpy(dtype=float)))
        ln_close = float(ind.close.iloc[left_neck])
        if (ls_close - ln_close) / ls_close < 0.05:
            return None
        ls_rsi = float(rsi.iloc[left_shoulder])
        head_rsi = float(rsi.iloc[head])
        if not np.isfinite([ls_rsi, head_rsi]).all() or ls_rsi - head_rsi < 2.0:
            return None
        max_rs = min(current - 2, left_shoulder + 120, head + int(2.5 * (head - left_shoulder)))
        for right_shoulder in range(head + 4, max_rs + 1):
            rs_close = float(ind.close.iloc[right_shoulder])
            if rs_close >= ls_close:
                continue
            if float(ind.close.iloc[right_shoulder + 1]) >= rs_close or float(ind.close.iloc[right_shoulder + 2]) >= rs_close:
                continue
            right_slice = ind.close.iloc[head + 1 : right_shoulder]
            if right_slice.empty:
                continue
            right_neck = head + 1 + int(np.argmin(right_slice.to_numpy(dtype=float)))
            if right_shoulder - right_neck < 3 or right_shoulder - right_neck > 50:
                continue
            rn_close = float(ind.close.iloc[right_neck])
            if (head_close - rn_close) / head_close < 0.05:
                continue
            skew = (rn_close - ln_close) / ln_close
            if skew > 0.10 or abs(skew) > 0.30:
                continue
            slope = (rn_close - ln_close) / (right_neck - left_neck)
            neckline_at = lambda idx: ln_close + slope * (idx - left_neck)
            head_neckline = neckline_at(head)
            if head_neckline <= 0 or (head_close - head_neckline) / head_neckline < 0.10:
                continue
            rs_neckline = neckline_at(right_shoulder)
            if rs_neckline <= 0 or (rs_close - rs_neckline) / rs_neckline < 0.05:
                continue
            rs_rsi = float(rsi.iloc[right_shoulder])
            if not np.isfinite(rs_rsi) or rs_rsi >= head_rsi or rs_rsi > 60.0:
                continue
            if right_shoulder - left_shoulder < 20 or right_shoulder - left_shoulder > 120:
                continue
            break_index: int | None = None
            consecutive = 0
            invalid = False
            for idx in range(right_shoulder + 1, current + 1):
                if float(ind.close.iloc[idx]) > head_close:
                    invalid = True
                    break
                consecutive = consecutive + 1 if float(ind.close.iloc[idx]) < neckline_at(idx) else 0
                if consecutive == 2:
                    break_index = idx
                    break
            if invalid:
                continue
            day_seven = right_shoulder + 7
            entry = min(day_seven, break_index) if break_index is not None else day_seven
            if entry != current:
                continue
            neckline = neckline_at(entry)
            target = neckline - (head_close - neckline)
            if target <= 0:
                continue
            return _Setup(left_shoulder, left_neck, head, right_neck, right_shoulder, entry, neckline, target)
        return None
