from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from patterns._rules import extrema


@dataclass(frozen=True)
class ChannelSetup:
    first: int
    second: int
    turn: int
    start: int
    first_price: float
    second_price: float
    turn_price: float
    start_price: float
    slope: float
    width: float
    entry: int
    entry_line: float
    first_rsi: float
    second_rsi: float
    break_rsi: float


def find_channel(ind: IndicatorEngine, rsi, current: int, direction: Literal["up", "down"]) -> ChannelSetup | None:
    kind = "high" if direction == "up" else "low"
    swings = extrema(ind.high if direction == "up" else ind.low, kind, 2)
    for second in reversed(swings):
        if second + 2 > current:
            continue
        for first in reversed([idx for idx in swings if idx < second]):
            setup = evaluate_channel(ind, rsi, current, first, second, direction)
            if setup is not None and setup.entry == current:
                return setup
    return None


def evaluate_channel(ind: IndicatorEngine, rsi, current: int, first: int, second: int, direction: str) -> ChannelSetup | None:
    gap = second - first
    if gap < 20 or gap > 180:
        return None
    first_price = float((ind.high if direction == "up" else ind.low).iloc[first])
    second_price = float((ind.high if direction == "up" else ind.low).iloc[second])
    first_rsi = float(rsi.iloc[first])
    second_rsi = float(rsi.iloc[second])
    if not np.isfinite([first_rsi, second_rsi]).all():
        return None
    if direction == "up":
        if second_price < first_price * 1.02 or first_rsi < 55.0:
            return None
        if second_rsi < 35.0 or second_rsi > 75.0 or first_rsi - second_rsi < 5.0:
            return None
        start_lo = max(0, first - 200)
        prior = ind.low.iloc[start_lo:first]
        if prior.empty:
            return None
        start = start_lo + int(np.argmin(prior.to_numpy(dtype=float)))
        start_price = float(ind.low.iloc[start])
        if start_price <= 0 or (first_price - start_price) / start_price < 0.15:
            return None
        turn_slice = ind.low.iloc[first + 1 : second]
        if turn_slice.empty:
            return None
        turn = first + 1 + int(np.argmin(turn_slice.to_numpy(dtype=float)))
        turn_price = float(ind.low.iloc[turn])
        if turn_price <= start_price:
            return None
        depth = (first_price - turn_price) / first_price
        if depth < 0.02 or depth > 0.25:
            return None
        between = ind.close.iloc[first + 1 : second]
        if not between.empty and float(between.max()) > second_price:
            return None
    else:
        if second_price > first_price * 0.98 or first_rsi > 45.0:
            return None
        if second_rsi < 25.0 or second_rsi > 65.0 or second_rsi - first_rsi < 5.0:
            return None
        start_lo = max(0, first - 200)
        prior = ind.high.iloc[start_lo:first]
        if prior.empty:
            return None
        start = start_lo + int(np.argmax(prior.to_numpy(dtype=float)))
        start_price = float(ind.high.iloc[start])
        if start_price <= 0 or (start_price - first_price) / start_price < 0.15:
            return None
        turn_slice = ind.high.iloc[first + 1 : second]
        if turn_slice.empty:
            return None
        turn = first + 1 + int(np.argmax(turn_slice.to_numpy(dtype=float)))
        turn_price = float(ind.high.iloc[turn])
        if turn_price >= start_price:
            return None
        height = (turn_price - first_price) / first_price
        if height < 0.02 or height > 0.25:
            return None
        between = ind.close.iloc[first + 1 : second]
        if not between.empty and float(between.min()) < second_price:
            return None
    slope = (second_price - first_price) / gap
    if direction == "up":
        parallel_at_turn = first_price + slope * (turn - first)
        width = parallel_at_turn - turn_price
        line = lambda idx: turn_price + slope * (idx - turn)
    else:
        parallel_at_turn = first_price + slope * (turn - first)
        width = turn_price - parallel_at_turn
        line = lambda idx: turn_price + slope * (idx - turn)
    if width <= 0:
        return None
    consecutive = 0
    entry: int | None = None
    for idx in range(second + 1, current + 1):
        close = float(ind.close.iloc[idx])
        if direction == "up" and close > second_price:
            return None
        if direction == "down" and close < second_price:
            return None
        crossed = close < line(idx) if direction == "up" else close > line(idx)
        consecutive = consecutive + 1 if crossed else 0
        if consecutive == 2:
            entry = idx
            break
    if entry is None:
        return None
    break_rsi = float(rsi.iloc[entry])
    if not np.isfinite(break_rsi):
        return None
    if direction == "up" and break_rsi >= second_rsi:
        return None
    if direction == "down" and break_rsi <= second_rsi:
        return None
    return ChannelSetup(
        first,
        second,
        turn,
        start,
        first_price,
        second_price,
        turn_price,
        start_price,
        float(slope),
        float(width),
        entry,
        float(line(entry)),
        first_rsi,
        second_rsi,
        break_rsi,
    )
