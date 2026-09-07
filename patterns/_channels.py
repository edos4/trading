from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from analysis.indicator_engine import IndicatorEngine
from patterns import _dedup
from patterns._rules import extrema

# `.cjs` backtest_uc_v14.cjs constants (short channel break) — the mirror
# `down`/long path reuses the same structure with the thresholds flipped.
LB = 3
MIN_GAP = 20
MAX_GAP = 180
UPTREND_MIN = 0.15
SH1_RSI_MIN = 55.0
SH2_ABOVE = 1.02
RSI_DIV_MIN = 5.0
SH2_RSI_MIN = 35.0
SH2_RSI_MAX = 75.0
VALLEY_MIN = 0.02
VALLEY_MAX = 0.25
NECK_SCAN_DAYS = 30
NECK_CONFIRM = 2

# Per-symbol memo for _structure / _find_break (both invariant across the walk
# for a given pivot pair). Keyed by _dedup.generation(), cleared each symbol.
_memo_gen: int = -1
_st_memo: dict = {}
_brk_memo: dict = {}


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


@dataclass(frozen=True)
class _Structure:
    turn: int
    turn_price: float
    start: int
    start_price: float
    slope: float
    width: float
    first_price: float
    second_price: float
    first_rsi: float
    second_rsi: float


def _structure(
    ind: IndicatorEngine, rsi, first: int, second: int, direction: str
) -> _Structure | None:
    """The pattern's static geometry (`.cjs` C1-C12), ignoring the break."""
    gap = second - first
    if gap < MIN_GAP or gap > MAX_GAP:
        return None
    up = direction == "up"
    peak = ind.high if up else ind.low
    first_price = float(peak.iloc[first])
    second_price = float(peak.iloc[second])
    first_rsi = float(rsi.iloc[first])
    second_rsi = float(rsi.iloc[second])
    if not np.isfinite([first_rsi, second_rsi]).all():
        return None

    if up:
        if second_price < first_price * SH2_ABOVE or first_rsi < SH1_RSI_MIN:
            return None
        if second_rsi >= first_rsi or first_rsi - second_rsi < RSI_DIV_MIN:
            return None
        if second_rsi < SH2_RSI_MIN or second_rsi > SH2_RSI_MAX:
            return None
        prior = ind.low.iloc[max(0, first - 200):first]
        if prior.empty:
            return None
        start = max(0, first - 200) + int(np.argmin(prior.to_numpy(dtype=float)))
        start_price = float(ind.low.iloc[start])
        if start_price <= 0 or (first_price - start_price) / start_price < UPTREND_MIN:
            return None
        turn_slice = ind.low.iloc[first + 1 : second]
        if turn_slice.empty:
            return None
        turn = first + 1 + int(np.argmin(turn_slice.to_numpy(dtype=float)))
        turn_price = float(ind.low.iloc[turn])
        if turn_price <= start_price:
            return None
        depth = (first_price - turn_price) / first_price
        if depth < VALLEY_MIN or depth > VALLEY_MAX:
            return None
        between = ind.high.iloc[first + 1 : second]
        if not between.empty and float(between.max()) > second_price:
            return None
        width = second_price - turn_price
    else:
        if second_price > first_price * (2 - SH2_ABOVE) or first_rsi > (100 - SH1_RSI_MIN):
            return None
        if second_rsi <= first_rsi or second_rsi - first_rsi < RSI_DIV_MIN:
            return None
        if second_rsi < (100 - SH2_RSI_MAX) or second_rsi > (100 - SH2_RSI_MIN):
            return None
        prior = ind.high.iloc[max(0, first - 200):first]
        if prior.empty:
            return None
        start = max(0, first - 200) + int(np.argmax(prior.to_numpy(dtype=float)))
        start_price = float(ind.high.iloc[start])
        if start_price <= 0 or (start_price - first_price) / start_price < UPTREND_MIN:
            return None
        turn_slice = ind.high.iloc[first + 1 : second]
        if turn_slice.empty:
            return None
        turn = first + 1 + int(np.argmax(turn_slice.to_numpy(dtype=float)))
        turn_price = float(ind.high.iloc[turn])
        if turn_price >= start_price:
            return None
        height = (turn_price - first_price) / first_price
        if height < VALLEY_MIN or height > VALLEY_MAX:
            return None
        between = ind.low.iloc[first + 1 : second]
        if not between.empty and float(between.min()) < second_price:
            return None
        width = turn_price - second_price

    if width <= 0:
        return None
    slope = (second_price - first_price) / gap
    return _Structure(
        turn, turn_price, start, start_price, float(slope), float(width),
        first_price, second_price, first_rsi, second_rsi,
    )


def _find_break(ind: IndicatorEngine, rsi, second: int, current: int, st: _Structure,
                up: bool) -> int | None:
    """First 2-consecutive-close break of the rising rail (`.cjs` scanEnd = SH2+30)."""
    line = lambda idx: st.turn_price + st.slope * (idx - st.turn)
    consecutive = 0
    for idx in range(second + 1, min(current, second + NECK_SCAN_DAYS) + 1):
        close = float(ind.close.iloc[idx])
        if up and close > st.second_price:
            return None  # cancelled: SH2 reclaimed before the break
        if not up and close < st.second_price:
            return None
        crossed = close < line(idx) if up else close > line(idx)
        consecutive = consecutive + 1 if crossed else 0
        if consecutive >= NECK_CONFIRM:
            return idx
    return None


def _make_setup(ind, rsi, first, second, st, entry, up) -> ChannelSetup | None:
    break_rsi = float(rsi.iloc[entry])
    if not np.isfinite(break_rsi):
        return None
    if up and break_rsi >= st.second_rsi:
        return None
    if not up and break_rsi <= st.second_rsi:
        return None
    line_at = st.turn_price + st.slope * (entry - st.turn)
    return ChannelSetup(
        first, second, st.turn, st.start,
        st.first_price, st.second_price, st.turn_price, st.start_price,
        st.slope, st.width, entry, float(line_at),
        st.first_rsi, st.second_rsi, break_rsi,
    )


def find_channel(
    ind: IndicatorEngine, rsi, current: int, direction: Literal["up", "down"]
) -> ChannelSetup | None:
    """The (SH1, SH2) pair the `.cjs` outer-i1 / inner-i2 scan would commit to,
    but only when its 2-close break confirms on the current bar.

    For the earliest non-consumed SH1, `.cjs` takes the earliest SH2 that
    passes structure and breaks within 30 bars. To stay walk-forward-safe we
    only look past an SH2 candidate once its 30-bar break window has fully
    elapsed with no break — otherwise `.cjs` might still commit to it.
    """
    global _memo_gen, _st_memo, _brk_memo
    if _memo_gen != _dedup.generation():
        _memo_gen, _st_memo, _brk_memo = _dedup.generation(), {}, {}

    up = direction == "up"
    series = ind.high if up else ind.low
    swings = extrema(series, "high" if up else "low", LB, strict=True)
    n = len(ind.close)
    # `.cjs` loop bounds: SH1 < n-68, SH2 <= n-48, break <= n-16 — it never
    # anchors a pattern it can't fully simulate. Match those so the walk
    # doesn't form trailing-edge patterns `.cjs` skips.
    max_first = n - (MIN_GAP + NECK_SCAN_DAYS + 15 + LB)
    max_second = n - (NECK_SCAN_DAYS + 15 + LB)
    max_break = n - 16

    def _st(f: int, s: int):
        key = (f, s)
        if key not in _st_memo:
            _st_memo[key] = _structure(ind, rsi, f, s, direction)
        return _st_memo[key]

    def _brk(f: int, s: int, st):
        key = (f, s)
        if key not in _brk_memo:
            _brk_memo[key] = (
                None if st is None
                else _find_break(ind, rsi, s, min(n - 1, s + NECK_SCAN_DAYS), st, up)
            )
        return _brk_memo[key]

    def _first_valid_second(f: int, taken: set[int]) -> int | None:
        """The SH2 the `.cjs` inner loop commits to for SH1=f (or None).

        `taken` holds SH2s already claimed by an earlier SH1 this pass — the
        `.cjs` `usedSH2.has(i2)` skip.
        """
        for s in swings:
            gap = s - f
            if gap < MIN_GAP:
                continue
            if gap > MAX_GAP or s > max_second:
                return None
            if s + LB >= n or _dedup.used(s) or s in taken:
                continue
            st = _st(f, s)
            if st is None:
                continue
            b = _brk(f, s, st)
            if b is not None and b <= max_break:
                return s
        return None

    # `.cjs`'s outer loop is SH1-ascending and each SH1 claims exactly one SH2.
    # Resolve those claims once; the SH1 that fires on the current bar is the
    # one whose claimed SH2's break confirms now.
    # Replay the `.cjs` SH1-ascending outer loop: each SH1 claims its first
    # still-available SH2. The SH1 whose claimed break confirms on the current
    # bar is the one that fires.
    taken: set[int] = set()
    for first in swings:
        if first > max_first:
            break
        if _dedup.used(first):
            continue
        second = _first_valid_second(first, taken)
        if second is None:
            continue
        taken.add(second)
        entry = _brk(first, second, _st(first, second))
        if entry == current:
            setup = _make_setup(ind, rsi, first, second, _st(first, second), entry, up)
            if setup is not None:
                return setup
    return None


# Back-compat: single-shot evaluate for tests / the Explorer.
def evaluate_channel(
    ind: IndicatorEngine, rsi, current: int, first: int, second: int, direction: str
) -> ChannelSetup | None:
    up = direction == "up"
    st = _structure(ind, rsi, first, second, direction)
    if st is None:
        return None
    entry = _find_break(ind, rsi, second, current, st, up)
    if entry is None:
        return None
    return _make_setup(ind, rsi, first, second, st, entry, up)
