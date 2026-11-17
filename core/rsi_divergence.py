"""Causal RSI divergence paper strategy adapted from the Trading Mode reel.

The reel illustrates bearish regular divergence and a swing-to-support bracket.
Numeric choices and the mirrored bullish setup are documented in
docs/rsi-divergence.md; they are implementation choices, not a video transcript.
"""

from __future__ import annotations

from copy import deepcopy

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from indicators._signal import BaseIndicator, emit
from patterns.base_pattern import ANN_PATTERN, TradeSignal, ann_segment


POLICY = {
    "version": "rsi-divergence-v1",
    "source": "https://www.facebook.com/reel/1695224485293688",
    "rsi_period": 14,
    "rsi_method": "wilder",
    "pivot_left": 2,
    "pivot_right": 2,
    "min_pivot_gap": 5,
    "max_pivot_gap": 60,
    "confirmation_bars": 5,
    "stop_buffer_atr": 0.1,
    "entry": "first close beyond second pivot candle opposite extreme after pivot confirmation",
    "target": "intervening support/resistance",
    "bullish_mirror": True,
}


class RSIDivergenceIndicator(BaseIndicator):
    """Trade regular divergence only after both price swings are confirmed."""

    MIN_BARS = 30

    @property
    def name(self) -> str:
        return "indicator_rsi_divergence"

    @property
    def chart_description(self) -> str:
        return "RSI Divergence: opposing price and RSI swings, confirmed by a reversal close."

    def analyze(self, snapshot: MarketSnapshot, store: OHLCVStore) -> TradeSignal | None:
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or len(df) < self.MIN_BARS:
            return None
        values = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values[:, :4] <= 0).any():
            return None
        if ((values[:, 1] < values[:, [0, 2, 3]].max(axis=1)).any()
                or (values[:, 2] > values[:, [0, 1, 3]].min(axis=1)).any()):
            return None
        ind = IndicatorEngine(df)
        rsi = ind.rsi_wilder(POLICY["rsi_period"]).to_numpy()
        atr = float(ind.atr(14).iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            return None
        high, low, close = (df[key].to_numpy(dtype=float) for key in ("high", "low", "close"))
        left, right = POLICY["pivot_left"], POLICY["pivot_right"]
        last = len(df) - 1
        for action, prices in (("SELL", high), ("BUY", low)):
            # Strict pivots avoid choosing an arbitrary point on a flat top/bottom.
            pivots = []
            start = max(left, last - POLICY["max_pivot_gap"] - right - POLICY["confirmation_bars"])
            for i in range(start, len(df) - right):
                neighbors = np.concatenate((prices[i-left:i], prices[i+1:i+right+1]))
                if ((prices[i] > neighbors).all() if action == "SELL"
                        else (prices[i] < neighbors).all()):
                    pivots.append(i)
            if len(pivots) < 2:
                continue
            first, second = pivots[-2:]
            confirmed = second + right
            if not (POLICY["min_pivot_gap"] <= second - first <= POLICY["max_pivot_gap"]):
                continue
            if not (0 <= last - confirmed < POLICY["confirmation_bars"]):
                continue
            r1, r2 = float(rsi[first]), float(rsi[second])
            if not np.isfinite([r1, r2]).all():
                continue
            if action == "SELL":
                divergence = high[second] > high[first] and r2 < r1
                trigger = low[second]
                reversals = close[confirmed:] < trigger
                invalidated = (high[second+1:] > high[second]).any()
                stop = high[second] + POLICY["stop_buffer_atr"] * atr
                target = float(low[first+1:second].min())
            else:
                divergence = low[second] < low[first] and r2 > r1
                trigger = high[second]
                reversals = close[confirmed:] > trigger
                invalidated = (low[second+1:] < low[second]).any()
                stop = low[second] - POLICY["stop_buffer_atr"] * atr
                target = float(high[first+1:second].max())
            if not divergence or invalidated or not reversals[-1] or reversals[:-1].any():
                continue
            dates = [self.bar_date(df, i) for i in (first, second)]
            signal = emit(
                self, df, snapshot, action=action, stop=float(stop), target=target,
                setup_key=tuple(int(df.index[i].value) for i in (first, second)),
                notes=(f"RSI Divergence {'bearish' if action == 'SELL' else 'bullish'}: "
                       f"price {prices[first]:.4f} → {prices[second]:.4f}; "
                       f"Wilder RSI(14) {r1:.2f} → {r2:.2f}. "
                       f"Reversal close confirmed; target prior {'support' if action == 'SELL' else 'resistance'}."),
            )
            if signal is None:
                continue
            # emit rounds to the market's current four-decimal signal precision.
            if not (signal.take_profit < signal.price < signal.stop_loss if action == "SELL"
                    else signal.stop_loss < signal.price < signal.take_profit):
                continue
            signal.provenance = "rsi_divergence"
            signal.pattern_evidence = {
                "policy": deepcopy(POLICY), "direction": action,
                "pivots": [{"date": date, "price": float(prices[i]), "rsi": float(rsi[i])}
                           for date, i in zip(dates, (first, second))],
                "confirmed_date": self.bar_date(df, confirmed),
                "signal_date": self.bar_date(df, last),
                "reversal_level": float(trigger), "atr14": atr,
                "structural_stop": signal.stop_loss, "structural_target": signal.take_profit,
                "confidence_semantics": "rule match, not a calibrated win probability",
            }
            signal.chart_annotations.append(ann_segment(
                *dates, float(prices[first]), float(prices[second]), ANN_PATTERN,
                label="RSI Divergence",
            ))
            signal.chart_annotations.append({
                "type": "rsi_segment", "timeframe": snapshot.timeframe,
                "start_date": dates[0], "end_date": dates[1],
                "start_value": r1, "end_value": r2,
                "color": ANN_PATTERN, "label": "RSI Divergence",
            })
            return signal
        return None
