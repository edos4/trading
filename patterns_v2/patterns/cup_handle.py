"""Cup with handle, O'Neil / Bulkowski.

A prior advance of at least 30%, then a U-shaped cup at least seven weeks
wide and 12–50% deep, rims within 8% of each other. The handle is a one-to-four
week dip no deeper than the top third of the cup. Confirm with a close above
the higher rim. A close under the handle low before that cancels it. The
bearish twin (inverse cup with handle) is the mirror.
"""
import numpy as np

from .common import first_break, sig


def _u_shape(low, x0, x1, cup_low, depth):
    # ponytail: bottom-band occupancy, not a semicircle residual. Swap if V-cups get through.
    width = x1 - x0
    if width <= 0 or depth <= 0:
        return False
    near = np.flatnonzero(low[x0 : x1 + 1] <= cup_low + 0.20 * depth)
    if near.size < 0.30 * (width + 1):
        return False
    # A spike spends one or two bars at the extreme. A cup's bottom is a band.
    if near.max() - near.min() < 0.25 * width:
        return False
    mid = float(near.mean()) / width
    return 0.25 <= mid <= 0.75


def detect(
    h, l, c, a, P,
    rim_pct=0.08, min_depth=0.12, max_depth=0.50, trend_pct=0.30, lookback=40,
    min_retr=0.05, max_retr=0.33, min_w=35, max_w=180, min_handle=5, max_handle=20, wait=30,
):
    I, Pr, H, C = P
    out = []
    for k in range(3, len(I)):
        # left rim, cup low, right rim, handle low
        if list(H[k - 3 : k + 1]) != [True, False, True, False]:
            continue
        ai = a[C[k]]
        if not np.isfinite(ai) or ai <= 0:
            continue
        left, cup_low, right, handle = (Pr[k - 3], Pr[k - 2], Pr[k - 1], Pr[k])
        x0, x_cup, x1, x_h = (int(I[k - 3]), int(I[k - 2]), int(I[k - 1]), int(I[k]))
        cup_w, handle_w = x1 - x0, x_h - x1
        if not (min_w <= cup_w <= max_w) or not (min_handle <= handle_w <= max_handle) or handle_w >= cup_w:
            continue
        # The bowl's low sits in the middle, not glued to either rim.
        rim_sep = max(8, int(0.20 * cup_w))
        if x_cup - x0 < rim_sep or x1 - x_cup < rim_sep:
            continue
        top = max(left, right)
        depth = top - cup_low
        if depth <= 0 or abs(top) <= 0 or cup_low >= min(left, right):
            continue
        if (top - min(left, right)) / abs(top) > rim_pct:
            continue
        if not (min_depth <= depth / abs(top) <= max_depth):
            continue
        if (right - handle) <= 0 or not (min_retr <= (right - handle) / depth <= max_retr):
            continue
        lo_i = max(0, x0 - lookback)
        if x0 <= lo_i:
            continue
        base = float(np.min(l[lo_i:x0]))
        if not np.isfinite(base) or base == 0 or (left - base) / abs(base) < trend_pct:
            continue
        if h[x0 : x1 + 1].max() > top:
            continue
        if not _u_shape(l, x0, x1, cup_low, depth):
            continue
        j = first_break(c, C[k], C[k] + wait, lambda _j, y=top: y, floor=handle, h=h, l=l)
        if j is None:
            continue
        lines = [
            (x0, left, x_cup, cup_low),
            (x_cup, cup_low, x1, right),
            (x1, right, x_h, handle),
            (x1, top, j, top),
        ]
        points = [
            (x0, left, "left rim"),
            (x_cup, cup_low, "cup"),
            (x1, right, "right rim"),
            (x_h, handle, "handle"),
        ]
        out.append(sig("cup_handle", 1, x0, x_h, j, top, depth / ai, lines, points))
    return out
