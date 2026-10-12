"""Shared utilities for chart-pattern detectors."""
import numpy as np
from scipy.signal import lfilter

MIRROR = {
    "double_bottom": "double_top",
    "inv_head_shoulders": "head_shoulders",
    "bull_flag": "bear_flag",
    "bull_pennant": "bear_pennant",
    "cup_handle": "inv_cup_handle",
}

# Patterns listed here stay implemented but are left out of scans, the web
# index, and PATTERN_NAMES.
DISABLED = frozenset({
    "head_shoulders",
    "inv_head_shoulders",
})

# Every pattern id the package can emit. Anything that files output by pattern
# name (the CLI chart folders, the web index) should check against this.
PATTERN_NAMES = frozenset(
    set(MIRROR)
    | set(MIRROR.values())
    | {
        "ascending_triangle",
        "descending_triangle",
        "symmetrical_triangle",
        "falling_wedge",
        "rising_wedge",
        "ascending_channel",
        "descending_channel",
        "rectangle",
    }
) - DISABLED


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    a = 1.0 / n
    out = lfilter([a], [1, -(1 - a)], tr, zi=[tr[:n].mean() * (1 - a)])[0]
    out[: n - 1] = np.nan
    return out


def bar_sane(h, l, c, i):
    """A bar that traded: it has a range, and the close sits inside that range.

    A zero-range print is not a pivot or a breakout, even when the price
    changed from the prior session. A close outside [low, high] is a bad print.
    """
    i = int(i)
    hi, lo, cl = float(h[i]), float(l[i]), float(c[i])
    if not (np.isfinite(hi) and np.isfinite(lo) and np.isfinite(cl)) or hi < lo:
        return False
    scale = max(abs(hi), abs(lo), abs(cl), 1.0)
    if hi - lo <= 1e-8 * scale:
        return False
    return lo - 1e-8 * scale <= cl <= hi + 1e-8 * scale


def zigzag(h, l, c, a, mult=2.0):
    """Causal ZigZag. Returns (idx, price, is_high, conf) arrays.

    Bars that did not trade are ignored, so a bad print cannot become a pivot.
    Indices stay aligned with the caller's arrays.
    """
    n = len(h)
    piv, d = [], 0
    hi = lo = 0
    for i in range(n):
        if bar_sane(h, l, c, i):
            hi = lo = i
            break
    for i in range(hi + 1, n):
        if not bar_sane(h, l, c, i):
            continue
        if np.isnan(a[i]):
            hi = lo = i
            continue
        thr = mult * a[i]
        if d == 0:
            if h[i] > h[hi]:
                hi = i
            if l[i] < l[lo]:
                lo = i
            if h[hi] - l[i] >= thr and hi < i:
                piv.append((hi, h[hi], True, i))
                d = -1
                lo = i
            elif h[i] - l[lo] >= thr and lo < i:
                piv.append((lo, l[lo], False, i))
                d = 1
                hi = i
        elif d == 1:
            if h[i] >= h[hi]:
                hi = i
            elif h[hi] - l[i] >= thr:
                piv.append((hi, h[hi], True, i))
                d = -1
                lo = i
        else:
            if l[i] <= l[lo]:
                lo = i
            elif h[i] - l[lo] >= thr:
                piv.append((lo, l[lo], False, i))
                d = 1
                hi = i
    if not piv:
        return (np.array([], int), np.array([]), np.array([], bool), np.array([], int))
    I, P, H, C = zip(*piv)
    return np.array(I), np.array(P, float), np.array(H, bool), np.array(C)


def first_break(c, start, stop, level_fn, floor=None, margin=0.0, h=None, l=None):
    """First close beyond level_fn(j) by ``margin``. A tag of the line is not a break.

    Aborts if a close prints through ``floor`` first. A bar that did not trade
    is skipped: it cannot confirm the break or cancel it.
    """
    margin = float(margin)
    for j in range(start, min(stop, len(c))):
        if h is not None and l is not None and not bar_sane(h, l, c, j):
            continue
        if floor is not None and c[j] < floor:
            return None
        if c[j] > level_fn(j) + margin:
            return j
    return None


def sig(name, direction, start, end, signal_bar, level, score, lines=(), points=()):
    return dict(
        pattern=name,
        direction=direction,
        start=int(start),
        end=int(end),
        signal=int(signal_bar),
        level=float(level),
        score=float(score),
        lines=[(float(x0), float(y0), float(x1), float(y1)) for x0, y0, x1, y1 in lines],
        points=[(float(x), float(y), str(label)) for x, y, label in points],
    )


def trade_mask(h, l, c):
    """True where a bar actually traded. A zero-range print did not, even if the price changed."""
    h, l, c = (np.asarray(x, float) for x in (h, l, c))
    scale = np.maximum(np.maximum(np.abs(h), np.abs(l)), np.maximum(np.abs(c), 1.0))
    return np.abs(h - l) > 1e-8 * scale


def has_split(c, i0, i1, limit=0.40):
    """True if any close from i0 through i1 jumps more than `limit` versus the prior close.

    A one-day move past about 40% is a split or a bad print, not a pattern break.
    """
    i0, i1 = int(i0), int(i1)
    if i1 <= 0 or i0 > i1:
        return False
    lo = max(i0, 1)
    prev, cur = c[lo - 1 : i1], c[lo : i1 + 1]
    good = np.isfinite(prev) & np.isfinite(cur) & (prev != 0)
    rel = np.ones(cur.shape, float)
    np.divide(cur, prev, out=rel, where=good)
    return bool(np.any(good & (np.abs(rel - 1.0) > limit)))


def _broken_line(sig):
    """Boundary that broke: the line ending on the signal bar at ``level``."""
    j = int(sig["signal"])
    level = float(sig["level"])
    tol = 1e-6 * max(abs(level), 1.0)
    for x0, y0, x1, y1 in sig.get("lines") or ():
        if int(round(float(x1))) != j:
            continue
        if abs(float(y1) - level) <= tol:
            return float(x0), float(y0), float(x1), float(y1)
    return float(j), level, float(j), level


def _line_at(x0, y0, x1, y1, bar):
    span = x1 - x0
    if abs(span) < 1e-9:
        return y1
    return y0 + (y1 - y0) * ((bar - x0) / span)


def breakout_failed(c, sig):
    """True when a later close comes back through the broken boundary.

    An upside breakout fails when a later close falls to the resistance line.
    A breakdown fails when a later close rises to the support line. The scan
    drops that signal: the pattern did not hold, so the position is closed.
    ``detect`` stays causal and still reports the breakout bar.
    """
    c = np.asarray(c, float)
    j = int(sig["signal"])
    if j < 0 or j + 1 >= len(c):
        return False
    x0, y0, x1, y1 = _broken_line(sig)
    up = int(sig["direction"]) > 0
    for t in range(j + 1, len(c)):
        y = _line_at(x0, y0, x1, y1, t)
        if up and c[t] <= y:
            return True
        if not up and c[t] >= y:
            return True
    return False


def tradable(h, l, c, i0, i1, min_frac=0.70, min_n=12, gap=0.30):
    """Pattern window is real trades: enough ranged bars, and no split-sized gap.

    A preferred or penny series of unchanged prints fails even when a few
    needles poke out. The median active bar has to be a real fraction of price.
    """
    i0, i1 = int(i0), int(i1)
    if i1 <= i0 or i1 >= len(c):
        return False
    h, l, c = (np.asarray(x, float) for x in (h, l, c))
    act = trade_mask(h, l, c)[i0 : i1 + 1]
    if int(act.sum()) < min_n or float(act.mean()) < min_frac:
        return False
    rng = (h[i0 : i1 + 1] - l[i0 : i1 + 1])[act]
    price = float(np.median(np.abs(c[i0 : i1 + 1])))
    if price <= 0 or float(np.median(rng)) < 0.0015 * price:
        return False
    return not has_split(c, i0, i1, gap)
