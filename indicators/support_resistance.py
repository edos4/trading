"""Trade bounces at the automatic support/resistance rails, DII-gated.

The rails are the same pivot-anchored trend lines drawn on trade charts. The
strategy only considers a rail bounce while the DII bullish percentage sits in
its 40%–60% consideration band, the same band the pattern scan gate enforces.
A BUY needs a touch/hold of confirmed, unbroken support; a SELL needs a
rejection at confirmed, unbroken resistance.
"""
from __future__ import annotations

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from analysis.pattern_dii import LENGTH, MAX_BULLS, MIN_BULLS, PERIOD, dii_consideration
from analysis.trendlines import detect_trendlines
from indicators._signal import BaseIndicator, emit, reward_bracket

TOUCH_ATR = 0.5
STOP_ATR = 0.5
TARGET_R = 2.0


class SupportResistanceIndicator(BaseIndicator):
    MIN_BARS = LENGTH + PERIOD + 2

    @property
    def name(self) -> str:
        return "indicator_support_resistance"

    @property
    def chart_description(self) -> str:
        return "Bounce at the automatic support/resistance rail, DII-confirmed"

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=self.MIN_BARS)
        if df is None or len(df) < self.MIN_BARS:
            return None
        engine = IndicatorEngine(df)
        bulls, bears = dii_consideration(engine.directional_imbalance_index(LENGTH, PERIOD))
        if not np.isfinite(bulls) or not MIN_BULLS <= bulls <= MAX_BULLS:
            return None
        atr = float(engine.atr().iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            return None
        lines = detect_trendlines(df)
        index = len(df) - 1
        price = float(df["close"].iloc[-1])
        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])
        opening = float(df["open"].iloc[-1])
        if not np.isfinite(price) or price <= 0:
            return None
        tolerance = max(atr * TOUCH_ATR, price * 0.002)
        for action, kind in (("BUY", "support"), ("SELL", "resistance")):
            rail = self._rail(lines, kind)
            if rail is None:
                continue
            level = self._level(rail, index)
            if not np.isfinite(level) or level <= 0:
                continue
            if action == "BUY":
                touched = level < price and low <= level + tolerance and price >= opening
                stop = min(low, level) - atr * STOP_ATR
            else:
                touched = level > price and high >= level - tolerance and price <= opening
                stop = max(high, level) + atr * STOP_ATR
            if not touched:
                continue
            target = self._target(lines, action, index, price, stop)
            if target is None:
                bracket = reward_bracket(action, price, stop, TARGET_R)
                if bracket is None:
                    continue
                target = bracket[1]
            return emit(
                self, df, snapshot, action=action, stop=stop, target=target,
                notes=(f"S/R {kind} {level:.4f} bounce; DII bulls {bulls:.2f}% "
                       f"bears {bears:.2f}%"),
                setup_key=(int(rail["start_index"]), 0 if kind == "support" else 1),
            )
        return None

    @staticmethod
    def _rail(lines, kind):
        return next(
            (line for line in lines if line["kind"] == kind
             and line["confirmed"] and not line["broken"]), None,
        )

    @staticmethod
    def _level(line, index):
        return float(line["start_price"] + line["slope"] * (index - line["start_index"]))

    @classmethod
    def _target(cls, lines, action, index, price, stop):
        line = cls._rail(lines, "resistance" if action == "BUY" else "support")
        if line is None:
            return None
        level = cls._level(line, index)
        risk = abs(price - stop)
        if not np.isfinite(level) or risk <= 0:
            return None
        if action == "BUY" and level > price and level - price >= risk:
            return level
        if action == "SELL" and level < price and price - level >= risk:
            return level
        return None
