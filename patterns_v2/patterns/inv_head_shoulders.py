"""Inverse head & shoulders, Bulkowski identification guidelines.

Three valleys after a downtrend measured before the left shoulder. The middle
valley (head) sits clearly below two shoulders that bottom within about 30%
of the pattern height of each other.
Neckline joins the two armpits. Confirm with a close above a flat or
down-sloping neckline; when the neckline slopes up, confirm above the right
armpit instead. The bearish mirror passes ``join_neck`` so a mildly
down-sloping top confirms on the line through both troughs. Once that drop
exceeds a small share of the height, confirm at the lower trough instead of
chasing the line. A close under the head before confirmation cancels it.
Each pivot whose close gives the extreme back by more than a quarter of the
pattern height is a wick, not a shoulder, head, or neck. The right shoulder
cannot be the next session after the armpit.
"""
import numpy as np

from .common import bar_sane, first_break, has_split, sig


def _printed(h, l, c, idx, is_low):
    """First bar that traded this extreme. ZigZag keeps the last equal print."""
    series = l if is_low else h
    idx = int(idx)
    extreme = float(series[idx])
    dust = 1e-8 * max(abs(extreme), 1.0)
    while idx > 0 and bar_sane(h, l, c, idx - 1) and abs(float(series[idx - 1]) - extreme) <= dust:
        idx -= 1
    return idx


def _rejected_wick(h, l, c, idx, is_low, height):
    """Close left the extreme, and that miss is a real share of the pattern."""
    if height <= 0:
        return True
    bar = _printed(h, l, c, idx, is_low)
    if not bar_sane(h, l, c, bar):
        return True
    hi, lo, cl = float(h[bar]), float(l[bar]), float(c[bar])
    span = hi - lo
    if span <= 0:
        return True
    wick = (cl - lo) if is_low else (hi - cl)
    if wick / span <= 0.50:
        return False
    return wick > 0.25 * height


def _isolated(h, l, idx, extreme, height, is_low):
    """One-bar spike: neither neighbor prints in the outer third of the swing."""
    idx = int(idx)
    if idx <= 0 or idx >= len(h) - 1 or height <= 0:
        return True
    if is_low:
        ceiling = float(extreme) + 0.33 * height
        return float(l[idx - 1]) > ceiling and float(l[idx + 1]) > ceiling
    floor = float(extreme) - 0.33 * height
    return float(h[idx - 1]) < floor and float(h[idx + 1]) < floor


def detect(
    h, l, c, a, P,
    shoulder_pct=0.08, time_ratio=2.0, trend_pct=0.10, head_frac=0.40,
    min_depth_atr=3.0, min_depth_pct=0.06,
    min_w=20, max_w=160, wait=40, join_neck=False,
):
    I, Pr, H, C = P
    out = []
    for k in range(5, len(I)):
        # trend high, L shoulder, armpit, head, armpit, R shoulder
        if list(H[k - 5 : k + 1]) != [True, False, True, False, True, False]:
            continue
        ai = a[C[k]]
        if not np.isfinite(ai) or ai <= 0:
            continue
        if not (min_w <= I[k] - I[k - 4] <= max_w):
            continue
        prior, ls, a1, hd, a2, rs = (Pr[k - 5], Pr[k - 4], Pr[k - 3], Pr[k - 2], Pr[k - 1], Pr[k])
        left_span = I[k - 2] - I[k - 4]
        right_span = I[k] - I[k - 2]
        if min(left_span, right_span) <= 0:
            continue
        if max(left_span, right_span) / min(left_span, right_span) > time_ratio:
            continue
        # shoulder_pct (share of price) is ignored. Shoulder gap is versus
        # pattern height, below. 8% of a $400 stock still hides a lopsided pair.
        neck_lo = min(a1, a2)
        if ls >= neck_lo or rs >= neck_lo:
            continue
        neck_avg = (a1 + a2) / 2.0
        relief = neck_avg - max(ls, rs)
        protrude = min(ls, rs) - hd
        depth = neck_avg - hd
        if relief <= 0 or protrude <= 0 or protrude < head_frac * relief:
            continue
        if depth < min_depth_atr * ai or depth < min_depth_pct * abs(hd):
            continue
        # The required decline precedes the formation. The later head cannot
        # supply the missing trend (INGR, JAPSY, WMK; mirrored for tops).
        if abs(ls) <= 0 or (prior - ls) / abs(ls) < trend_pct or prior <= max(a1, a2):
            continue
        if abs(a2 - a1) > (1.0 / 3.0) * depth:
            continue
        # Shoulders match against the smaller height (head to the lower armpit).
        # On the bearish mirror that is head minus the higher trough.
        span_h = float(neck_lo - hd)
        if span_h <= 0 or abs(float(ls - rs)) > 0.30 * span_h:
            continue
        i_ls, i_a1, i_hd, i_a2, i_rs = (
            int(I[k - 4]), int(I[k - 3]), int(I[k - 2]), int(I[k - 1]), int(I[k]),
        )
        # Armpits are bars strictly between the valleys, not the extreme bar itself.
        # The next session after the armpit is a bounce, not a shoulder
        # (BNPQF, FBIO, INN-PE).
        if not (i_ls + 1 < i_a1 < i_hd and i_hd < i_a2 < i_rs):
            continue
        if i_rs < i_a2 + 2:
            continue
        # A close that gives the extreme back, by a real share of the pattern
        # height, is a wick. A few cents off the high on a wide bar is not (NWL).
        if any(
            _rejected_wick(h, l, c, idx, is_low, span_h)
            for idx, is_low in (
                (i_ls, True), (i_hd, True), (i_rs, True), (i_a1, False), (i_a2, False),
            )
        ):
            continue
        # The head is the lowest low between the shoulders. A wick under the
        # labelled head is a different valley (FRAF, OKTA).
        dust_px = 1e-8 * max(abs(float(hd)), 1.0)
        traded = [t for t in range(i_ls, i_rs + 1) if bar_sane(h, l, c, t)]
        if not traded or float(min(l[t] for t in traded)) < float(hd) - dust_px:
            continue
        # A flat bottom is not a head. Few bars between the shoulders may reach the head's stick-out.
        deeper = min(ls, rs)
        stick = deeper - hd
        if stick <= 0:
            continue
        window = l[i_ls : i_rs + 1]
        ceiling = deeper - 0.25 * stick
        if window.size == 0 or np.sum(window <= ceiling) / window.size > 0.40:
            continue
        if _isolated(h, l, i_ls, ls, span_h, True) or _isolated(h, l, i_hd, hd, span_h, True) or _isolated(h, l, i_rs, rs, span_h, True):
            continue
        x0, y0 = I[k - 3], a1
        slope = (a2 - a1) / (I[k - 1] - I[k - 3])
        # join_neck: bearish mirror. A mildly down-sloping neck confirms on the
        # line through both troughs. A neck that has already fallen more than
        # a small share of the height confirms at the lower trough; extending
        # the line another month is not the break (EXP). The horizontal-at-the-
        # right-armpit rule is for a bullish inverse whose neckline rises.
        up_neck = (not join_neck) and a2 > a1
        steep = bool(join_neck and slope > 0 and (a2 - a1) > 0.15 * depth)
        if up_neck or steep:
            level_fn = lambda j, y=a2: y
        else:
            level_fn = lambda j, y0=y0, slope=slope, x0=x0: y0 + slope * (j - x0)
        # The head has to hold. A close through the neckline while the right
        # shoulder is still forming is an early break, not a later confirmation.
        left_arm, right_arm = int(I[k - 3]), int(I[k - 1])
        # Same margin as confirmation. A tag of the neckline while the right
        # shoulder forms is not an early break; a close through it is.
        neck_clear = 0.10 * ai
        if any(
            bar_sane(h, l, c, t) and c[t] > float(level_fn(t)) + neck_clear
            for t in range(left_arm, right_arm + 1)
        ):
            continue
        j = first_break(c, C[k], C[k] + wait, level_fn, floor=hd, margin=neck_clear, h=h, l=l)
        if j is None or j <= i_rs:
            continue
        # No new valley under the right shoulder before the break (FSBW on the mirror).
        if any(
            bar_sane(h, l, c, t) and float(l[t]) < float(rs) - dust_px
            for t in range(i_rs + 1, j)
        ):
            continue
        if has_split(c, i_ls, j):
            continue
        break_y = float(level_fn(j))
        if up_neck or steep:
            neck_line = (I[k - 1], a2, j, a2)
        else:
            neck_line = (x0, y0, j, break_y)
        lines = [
            (I[k - 5], prior, I[k - 4], ls),
            (I[k - 4], ls, I[k - 3], a1),
            (I[k - 3], a1, I[k - 2], hd),
            (I[k - 2], hd, I[k - 1], a2),
            (I[k - 1], a2, I[k], rs),
            (x0, y0, I[k - 1], a2),
            neck_line,
        ]
        points = [
            (I[k - 5], prior, "trend"),
            (I[k - 4], ls, "L shoulder"),
            (I[k - 3], a1, "neck"),
            (I[k - 2], hd, "head"),
            (I[k - 1], a2, "neck"),
            (I[k], rs, "R shoulder"),
        ]
        out.append(sig("inv_head_shoulders", 1, I[k - 4], I[k], j, break_y, depth / ai, lines, points))
    return out
