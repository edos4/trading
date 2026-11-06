"""Double-bottom reversal with a preceding decline and a confirmed neckline.

Two support tests, within 3% of price, separated by a rally of at least 10%.
The prior high must clear the neckline by 10% and lead a decline lasting at
least five bars. Minor swings may subdivide that decline or the W itself.
Adjacent prints in a bottom zone belong to one test; another test requires
an intervening recovery through half the remaining distance to the neckline.

These numerical defaults are detection policy, not universal charting rules.
The bearish double top uses this detector on negated prices.
"""
from __future__ import annotations

import numpy as np

from .common import first_break, has_split, sane_mask, sig


def _support_tests(
    l: np.ndarray, c: np.ndarray, sane: np.ndarray,
    start: int, stop: int, ceiling: float, recovery: float,
) -> list[tuple[int, int]]:
    """Group bottom-zone prints until a separate recovery has taken place.

    A recovery bar must leave the support zone completely. A wide candle
    touching both support and recovery cannot establish their intrabar order.
    """
    tests: list[tuple[int, int]] = []
    recovered = True
    for t in range(start, stop):
        if not sane[t]:
            continue
        if l[t] <= ceiling:
            if recovered:
                tests.append((t, t))
            else:
                tests[-1] = (tests[-1][0], t)
            recovered = False
        elif c[t] >= recovery:
            recovered = True
    return tests


def _trend_high(
    h: np.ndarray, c: np.ndarray, sane: np.ndarray, first: int,
    low: float, neck: float, lookback: int, min_bars: int,
    trend_pct: float, base_pct: float,
) -> int | None:
    """Find the high of a material decline before the first bottom."""
    start = max(0, first - lookback)
    xs = np.flatnonzero(sane[start:first]) + start
    if not len(xs):
        return None
    prior = float(np.max(h[xs]))
    # Use the last equal high: a flat shelf followed by one plunge is not a
    # multi-session decline just because the shelf began a long time ago.
    idx = int(xs[np.flatnonzero(h[xs] == prior)[-1]])
    if first - idx < min_bars or prior <= neck:
        return None
    if (prior - low) / abs(low) < trend_pct or (prior - neck) / abs(neck) < base_pct:
        return None
    traded = np.flatnonzero(sane[idx:first + 1]) + idx
    if len(traded) < min_bars + 1:
        return None
    third = max(1, len(traded) // 3)
    # Endpoint wicks or one final gap cannot supply the whole decline. The
    # early and late thirds must also fall by half the minimum trend move.
    if np.median(c[traded[:third]]) - np.median(c[traded[-third:]]) < 0.5 * trend_pct * abs(low):
        return None
    return idx


def detect(
    h: np.ndarray, l: np.ndarray, c: np.ndarray, a: np.ndarray,
    P: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    tol_pct: float = 0.03, peak_pct: float = 0.10,
    trend_pct: float = 0.10, base_pct: float = 0.10,
    min_w: int = 10, max_w: int = 120, wait: int = 40, hold: int = 0,
    trend_lookback: int = 60, min_trend_bars: int = 5,
) -> list[dict]:
    """Return causal confirmations; ``wait`` counts bars after the second low.

    ``hold`` optionally requires additional closes above the neckline. The
    signal then belongs to the last required close, when that fact is known.
    """
    if hold < 0 or wait < 1:
        raise ValueError("hold must be nonnegative and wait must be positive")
    I, Pr, H, C = P
    out: list[dict] = []
    sane = sane_mask(h, l, c)
    lows = np.flatnonzero(~H)
    for pos, k in enumerate(lows):
        i_lo2, confirmed = int(I[k]), int(C[k])
        if not (0 <= i_lo2 < confirmed < len(c)) or not sane[i_lo2]:
            continue
        ai = float(a[confirmed])
        if not np.isfinite(ai) or ai <= 0:
            continue
        for prev in lows[:pos][::-1]:
            i_lo1 = int(I[prev])
            width = i_lo2 - i_lo1
            if width > max_w:
                break
            if width < min_w or not sane[i_lo1]:
                continue
            lo1, lo2 = float(Pr[prev]), float(Pr[k])
            ref = abs((lo1 + lo2) / 2.0)
            if not np.isfinite(ref) or ref <= 0 or lo1 == 0 or lo2 == 0:
                continue
            if abs(lo2 - lo1) / ref > tol_pct:
                continue
            inside = np.flatnonzero(sane[i_lo1 + 1:i_lo2]) + i_lo1 + 1
            if not len(inside):
                continue
            i_neck = int(inside[np.argmax(h[inside])])
            neck = float(h[i_neck])
            if neck == 0 or (neck - max(lo1, lo2)) / ref < peak_pct:
                continue
            if not (i_lo1 + 1 < i_neck < i_lo2 - 1):
                continue
            floor = min(lo1, lo2)
            height = neck - floor
            dust = 1e-8 * max(abs(floor), 1.0)
            # A hidden lower valley cannot be skipped when pairing pivots.
            if float(np.min(l[inside])) < floor - dust:
                continue
            i_prior = _trend_high(
                h, c, sane, i_lo1, lo1, neck, trend_lookback,
                min_trend_bars, trend_pct, base_pct,
            )
            if i_prior is None:
                continue
            # Search from the low, so a break before ZigZag confirmation is
            # never forgotten and reissued later as a fresh breakout.
            deadline = i_lo2 + wait + 1
            j = first_break(c, i_lo2 + 1, deadline, lambda _: neck,
                            floor=floor, margin=0.05 * height, h=h, l=l)
            if j is None:
                continue
            if j < confirmed:
                # The pivot is not yet known. Delay to its confirmation only
                # if the original break held throughout the intervening bars.
                if confirmed >= deadline or not sane[confirmed]:
                    continue
                xs = np.flatnonzero(sane[j:confirmed + 1]) + j
                if np.any(c[xs] <= neck) or c[confirmed] <= neck + 0.05 * height:
                    continue
                j = confirmed
            shallow = max(lo1, lo2)
            ceiling = shallow + tol_pct * ref
            recovery = ceiling + 0.5 * (neck - ceiling)
            # Include the decline: otherwise the last two valleys of a triple
            # bottom could borrow an old high and masquerade as a fresh W.
            tests = _support_tests(l, c, sane, i_prior, j, ceiling, recovery)
            if len(tests) != 2 or not (tests[0][0] <= i_lo1 <= tests[0][1]
                                      and tests[1][0] <= i_lo2 <= tests[1][1]):
                continue
            # A new lower low is a new setup, even when its bar closes back
            # above the neckline. Equal prints may still form one bottom.
            after = np.flatnonzero(sane[i_lo2 + 1:j + 1]) + i_lo2 + 1
            if len(after) and float(np.min(l[after])) < floor - dust:
                continue
            if float(h[j] - l[j]) > 2.0 * height:
                continue
            signal_bar = j + hold
            if signal_bar >= len(c):
                continue
            if hold and (not sane[j + 1:signal_bar + 1].all()
                         or not (c[j + 1:signal_bar + 1] > neck).all()):
                continue
            if hold and np.any(l[j + 1:signal_bar + 1] < floor - dust):
                continue
            if has_split(c, i_prior, signal_bar, valid=sane):
                continue
            prior = float(h[i_prior])
            lines = [
                (i_prior, prior, i_lo1, lo1),
                (i_lo1, lo1, i_neck, neck),
                (i_neck, neck, i_lo2, lo2),
                (i_neck, neck, signal_bar, neck),
            ]
            points = [
                (i_prior, prior, "trend"), (i_lo1, lo1, "low 1"),
                (i_neck, neck, "neck"), (i_lo2, lo2, "low 2"),
            ]
            out.append(sig("double_bottom", 1, i_lo1, i_lo2, signal_bar,
                           neck, height / ai, lines, points))
    return out
