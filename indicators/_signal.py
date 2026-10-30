"""Turn a finished indicator reading into the same TradeSignal the explorer draws."""

from __future__ import annotations

import numpy as np

from patterns._rules import notional_qty
from patterns.base_pattern import (
    ANN_ENTRY,
    ANN_STOP,
    ANN_TARGET,
    BasePattern,
    TradeSignal,
    ann_hline,
    ann_marker,
    ann_path,
)


class BaseIndicator(BasePattern):
    """Detector that fires on the latest bar only, same contract as a pattern."""

    POSITION_NOTIONAL = 10_000.0
    # Trend indicators trail their own line. Pivots keep a fixed S1/R1 bracket.
    trails = False

    @property
    def timeframes(self) -> list[str]:
        return ["1d", "1W"]

    @property
    def chart_description(self) -> str:
        return self.name

    def trail(self, df) -> tuple[str, float] | None:
        """Current (BUY|SELL, stop) while this indicator's trend holds.

        None means the trend is gone and an open trade should exit. Indicators
        with ``trails`` false are not called.
        """
        return None


def reward_bracket(action: str, price: float, stop: float, reward_r: float = 2.0):
    """Stop on the protective side of price, target at ``reward_r`` times the risk."""
    if not np.isfinite(price) or not np.isfinite(stop) or price <= 0:
        return None
    risk = abs(price - stop)
    if risk <= 0:
        return None
    if action == "BUY" and stop < price:
        target = price + reward_r * risk
    elif action == "SELL" and stop > price:
        target = price - reward_r * risk
    else:
        return None
    # A band left over from a much higher price (or a sub-penny print) can
    # sit many multiples away and print a target through zero. Those are
    # not trades. Pattern scans do not call this.
    if stop <= 0 or target <= 0:
        return None
    return stop, target


def protective_stop(action: str, price: float, structural: float, atr: float) -> float:
    """Use the indicator level when it protects the trade, otherwise one ATR."""
    pad = float(atr) if np.isfinite(atr) and atr > 0 else abs(price) * 0.02
    if action == "BUY" and np.isfinite(structural) and structural < price:
        return float(structural)
    if action == "SELL" and np.isfinite(structural) and structural > price:
        return float(structural)
    if action == "BUY":
        return price - pad
    return price + pad


# A band left over from a much higher print can sit many times away from
# price. That is not an entry. Half the price is the farthest initial stop.
MAX_INITIAL_STOP_FRAC = 0.50


def emit(
    detector: BaseIndicator,
    df,
    snapshot,
    *,
    action: str,
    stop: float,
    target: float | None = None,
    notes: str,
    line=None,
    setup_key=None,
) -> TradeSignal | None:
    price = float(df["close"].iloc[-1])
    if action not in ("BUY", "SELL") or not np.isfinite(price) or price <= 0:
        return None
    if not np.isfinite(stop) or stop <= 0:
        return None
    # No target: the indicator line is the stop and it trails. A target is the
    # pivot bracket (S1/R1), which is that indicator's own fixed exit.
    trailing = target is None
    if trailing:
        if action == "BUY" and not stop < price:
            return None
        if action == "SELL" and not stop > price:
            return None
        if abs(price - stop) / price > MAX_INITIAL_STOP_FRAC:
            return None
    else:
        if not np.isfinite(target) or target <= 0:
            return None
        if action == "BUY" and not (stop < price < target):
            return None
        if action == "SELL" and not (target < price < stop):
            return None
    idx = len(df) - 1
    date = detector.bar_date(df, idx)
    marker = "^" if action == "BUY" else "v"
    label_pos = "below" if action == "BUY" else "above"
    annotations = [
        ann_marker(date, price, "entry", ANN_ENTRY, marker, label_pos),
        ann_hline(round(float(stop), 4), "stop", ANN_STOP),
    ]
    if not trailing:
        annotations.append(ann_hline(round(float(target), 4), "target", ANN_TARGET))
    if line is not None:
        start = max(0, idx - 59)
        points = []
        for i in range(start, idx + 1):
            value = float(line[i])
            if np.isfinite(value):
                points.append((detector.bar_date(df, i), value))
        if len(points) > 1:
            annotations.append(ann_path(points, label=detector.name))
    return TradeSignal(
        symbol=snapshot.symbol,
        action=action,
        pattern=detector.name,
        timeframe=snapshot.timeframe,
        confidence=1.0,
        price=round(price, 4),
        qty=notional_qty(detector.POSITION_NOTIONAL, price),
        stop_loss=round(float(stop), 4),
        stop_loss_on_close=trailing,
        take_profit=None if trailing else round(float(target), 4),
        notes=notes,
        setup_key=setup_key,
        chart_annotations=annotations,
    )

