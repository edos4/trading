"""Bull flag and bull pennant, Edwards & Magee / Bulkowski.

Both need a steep, nearly straight flagpole made of real trades, not one gap
across stale prints. A flag is a short (under three weeks) near-parallel
channel, flat or tilted against the pole but much less steep than the pole,
with both boundaries touched by the pause rather than by two wicks. A pennant is a short symmetrical triangle on
the same pole, narrowed to about
half its opening width. Retracement stays within 38% of the pole. The signal is
a close through the upper boundary that also clears the top of the flag by
one ATR. A close that only nicks a falling rail and stays in the same price
range is not a breakout. Bearish twins are mirrors.
"""
import numpy as np

from .common import bar_sane, sig, trade_mask


def _channel(h, l, xs):
    x = xs - xs[0]
    su, bu = np.polyfit(x, h[xs], 1)
    sl, bl = np.polyfit(x, l[xs], 1)
    bu += (h[xs] - (su * x + bu)).max()
    bl -= ((sl * x + bl) - l[xs]).max()
    x0 = float(xs[0])

    def up(bar, su=su, bu=bu, x0=x0):
        return su * (bar - x0) + bu

    def lo(bar, sl=sl, bl=bl, x0=x0):
        return sl * (bar - x0) + bl

    return float(su), float(sl), up, lo


def _pause_hugs(hi_gap, lo_gap):
    """True when both rails are touched and the typical bar sits on them.

    Two spikes must not open a channel the rest of the pause floats inside.
    """
    med_h, med_l = float(np.median(hi_gap)), float(np.median(lo_gap))
    return (
        np.sum(hi_gap <= 0.25) >= 2
        and np.sum(lo_gap <= 0.25) >= 2
        and max(med_h, med_l) <= 0.55
        and min(med_h, med_l) <= 0.33
    )


def _turns(c, xs):
    """Direction changes inside the pause. A straight run is not a flag or pennant."""
    d = np.diff(c[xs])
    s = np.sign(d)
    s = s[s != 0]
    if len(s) < 2:
        return 0
    return int(np.sum(s[1:] * s[:-1] < 0))


def _geometry(pattern_name, su, sl, up, lo, xs, move, ai, pole_slope):
    w0 = up(xs[0]) - lo(xs[0])
    w1 = up(xs[-1]) - lo(xs[-1])
    if w0 <= 0 or w1 <= 0:
        return False
    if (w0 + w1) / 2 > 0.5 * move:
        return False
    if pattern_name == "bull_flag":
        # Near-parallel, and flat or down (against the up pole). Not a rising channel.
        if abs(su - sl) > 0.04 * ai:
            return False
        if not (0.80 * w0 <= w1 <= 1.25 * w0):
            return False
        if 0.5 * (su + sl) > 0.02 * ai:
            return False
        # A flag is a pause. A counter-move nearly as steep as the pole is a reversal.
        if pole_slope <= 0 or abs(0.5 * (su + sl)) > 0.25 * pole_slope:
            return False
        return True
    if pattern_name == "bull_pennant":
        # Short symmetrical triangle: upper falls, lower rises, lines converge.
        if su > -0.01 * ai or sl < 0.01 * ai:
            return False
        if w1 > 0.50 * w0:
            return False
        return True
    return False


def detect(
    h, l, c, a, _P, pattern_name,
    pole_atr=8.0, max_pole=12, min_c=5, max_c=15, min_retr=0.12, max_retr=0.38, min_slope_atr=0.50,
    thrust_atr=1.0,
):
    n, out = len(c), []
    act = trade_mask(h, l, c)
    for i1 in range(max_pole, n - min_c):
        if h[i1] < h[i1 - 1] or h[i1] < h[i1 + 1]:
            continue
        if not bar_sane(h, l, c, i1):
            continue
        ai = a[i1]
        if not np.isfinite(ai) or ai <= 0:
            continue
        lo0 = i1 - max_pole
        i0 = lo0 + int(np.argmin(l[lo0 : i1 + 1]))
        if i1 - i0 < 3 or h[i1] < np.max(h[i0 : i1 + 1]):
            continue
        move = h[i1] - l[i0]
        if not np.isfinite(move) or move < pole_atr * ai:
            continue
        # ATR on a quiet preferred is a few cents, so a 2% drift clears it.
        # The pole also has to be a real percent of price (VBCF). Use abs:
        # the bearish mirror runs on negated prices.
        ref = max(abs(float(c[i1])), abs(float(l[i0])), 1.0)
        if move / ref < 0.08:
            continue
        if not bar_sane(h, l, c, i0):
            continue
        if move / (i1 - i0) < min_slope_atr * ai:
            continue
        run_max = np.maximum.accumulate(h[i0 : i1 + 1])
        if (run_max - l[i0 : i1 + 1]).max() > 0.35 * move:
            continue
        # A flagpole is a run of trades, not a flat print and one gap.
        pole = act[i0 : i1 + 1]
        if int(pole.sum()) < 4 or float(pole.mean()) < 0.50:
            continue
        if (h[i0 : i1 + 1] - l[i0 : i1 + 1]).max() > 0.55 * move:
            continue
        seg = c[i0 : i1 + 1]
        xx = np.arange(len(seg), dtype=float)
        pred = np.polyval(np.polyfit(xx, seg, 1), xx)
        ss_tot = float(((seg - seg.mean()) ** 2).sum())
        if ss_tot <= 0 or 1.0 - float(((seg - pred) ** 2).sum()) / ss_tot < 0.70:
            continue
        last = min(i1 + max_c, n - 1)
        for j in range(i1 + min_c, last + 1):
            xs = np.arange(i1 + 1, j)
            if len(xs) < min_c - 1:
                continue
            if h[xs].max() > h[i1]:
                break
            retr = (h[i1] - l[xs].min()) / move
            if retr > max_retr:
                break
            if retr < min_retr or _turns(c, xs) < 2:
                continue
            if int(act[xs].sum()) < 4 or float(act[xs].mean()) < 0.45:
                continue
            su, sl, up, lo_fn = _channel(h, l, xs)
            if not _geometry(pattern_name, su, sl, up, lo_fn, xs, move, ai, move / (i1 - i0)):
                continue
            upper = np.array([up(t) for t in xs])
            lower = np.array([lo_fn(t) for t in xs])
            width = upper - lower
            if np.any(width <= 0):
                continue
            # Both rails have to be touched, and the typical bar has to sit on them.
            # Two wicks must not open a channel the rest of the pause floats inside.
            hi_gap = (upper - h[xs]) / width
            lo_gap = (l[xs] - lower) / width
            if not _pause_hugs(hi_gap, lo_gap):
                continue
            if not act[j] or not bar_sane(h, l, c, j) or c[j] <= up(j) + 0.15 * ai:
                continue
            if pattern_name == "bull_flag":
                # The rail can slope down into the pause. Clearing that line
                # while the close is still inside the flag's highs is not a
                # move up.
                range_top = max(float(h[i1]), float(h[xs].max()))
                if c[j] <= range_top + thrust_atr * ai:
                    continue
            lines = [
                (i0, l[i0], i1, h[i1]),
                (xs[0], up(xs[0]), j, up(j)),
                (xs[0], lo_fn(xs[0]), j, lo_fn(j)),
            ]
            points = [(i0, l[i0], "pole"), (i1, h[i1], "pole top")]
            out.append(sig(pattern_name, 1, i0, j - 1, j, up(j), move / ai, lines, points))
            break
    return out
