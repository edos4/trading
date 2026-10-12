"""Bounded two-line patterns, Bulkowski identification guidelines.

Converging boundaries, price contained inside them, and at least five distinct
touches (three on one side and two on the other).

Triangles. Ascending: flat top, rising bottom. Descending: flat bottom, falling
top. A side is flat when it drifts by less than 4% of the pattern's average
height, and by less than about half of the other side's drift. A side that
is well outside that flat band stays sloped even when it is the slower one.
Symmetrical: falling top and rising bottom, long enough not to be a pennant.
A pivot that is a long wick, or that sits off its line by more than 8% of
the height, does not count. The first touch of each side has to fall in the
same window, and each third of the pattern has to come back to both rails.

Wedges. Both boundaries slope the same way and converge. A falling wedge
confirms on a close above the upper line; a rising wedge on a close below the
lower line. Breakout comes before the apex and not in the last 15% of the way
there, same as a triangle.

Channels and rectangles. Boundaries stay near parallel (width within 15%).
Channels slope up or down together; a rectangle is flat on both sides and
longer than a flag. Direction is whichever boundary price closes through.
"""
import numpy as np

from .common import bar_sane, has_split, sig

_NARROW = {
    "ascending_triangle",
    "descending_triangle",
    "symmetrical_triangle",
    "falling_wedge",
    "rising_wedge",
}
# Textbook confirmation side. Triangles, channels, and rectangles break either way.
_WANT = {"falling_wedge": 1, "rising_wedge": -1}


def _touch_x(idxs, prices, series):
    """First bar that printed this extreme.

    ZigZag keeps the last bar of a flat high/low. A sloped line through that
    later bar sits one step off the print. Walk back only across equal prices,
    which are already in the past when the pivot confirms.
    """
    out = []
    for i, p in zip(idxs, prices):
        i, p = int(i), float(p)
        tol = 1e-8 * max(abs(p), 1.0)
        while i > 0 and abs(float(series[i - 1]) - p) <= tol:
            i -= 1
        out.append(i)
    return out


def _fit_side(x, y, ai, touch_atr, upper):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    s, b = np.polyfit(x - x[0], y, 1)
    fitted = s * (x - x[0]) + b
    if upper:
        b += (y - fitted).max()
        gap = (s * (x - x[0]) + b) - y
    else:
        b -= (fitted - y).max()
        gap = y - (s * (x - x[0]) + b)
    if gap.max() > touch_atr * ai:
        return None
    return float(s), float(b), float(x[0])


def _kind(su, sl, w0, w1, span, flat):
    """Classify by drift across the whole pattern, not by slope per bar.

    A long line can move less than a fraction of ATR each day and still climb
    a large share of the pattern. ``flat`` is that share of the average width:
    a side is horizontal when its drift stays within ``flat`` of ``(w0 + w1) / 2``.
    ``su`` and ``sl`` are price per bar.

    A triangle's flat side also has to be flat relative to the sloping side.
    A top that falls by a quarter of the bottom's rise is a symmetrical
    triangle (or no triangle), not an ascending one.
    """
    if w0 <= 0 or w1 <= 0 or span <= 0:
        return None
    du, dl = su * span, sl * span
    lim = flat * 0.5 * (w0 + w1)

    def side(drift):
        if drift > lim:
            return 1
        if drift < -lim:
            return -1
        return 0

    nu, nl = side(du), side(dl)
    narrowing = w1 < 0.85 * w0
    parallel = 0.85 * w0 <= w1 <= 1.15 * w0
    if narrowing:
        # A slower side is the flat side of a triangle only when it is barely
        # outside the flat band. A floor that falls many dollars is a wedge
        # (NVMI), even if that fall is a small share of the other side.
        if nu == -1 and nl == -1 and du < dl and abs(dl) < 0.30 * abs(du) and abs(dl) <= 1.5 * lim:
            nl = 0
        elif nu == 1 and nl == 1 and dl > du and abs(du) < 0.30 * abs(dl) and abs(du) <= 1.5 * lim:
            nu = 0
        # Flat side may lean, but not enough to read as the other boundary.
        if nu == 0 and nl == 1 and du >= -0.25 * dl:
            return "ascending_triangle"
        if nl == 0 and nu == -1 and dl <= 0.25 * abs(du):
            return "descending_triangle"
        if nu == -1 and nl == 1:
            return "symmetrical_triangle"
        if nu == -1 and nl == -1 and du < dl:
            return "falling_wedge"
        if nu == 1 and nl == 1 and dl > du:
            return "rising_wedge"
        return None
    if not parallel:
        return None
    if nu == 1 and nl == 1:
        return "ascending_channel"
    if nu == -1 and nl == -1:
        return "descending_channel"
    if nu == 0 and nl == 0:
        return "rectangle"
    return None


def _marches(prices, avg_w):
    """True when every step of a 'flat' side goes the same way by a real amount."""
    prices = np.asarray(prices, float)
    if len(prices) < 3 or abs(float(prices[-1] - prices[0])) <= 0.05 * avg_w:
        return False
    steps = np.diff(prices)
    return bool(np.all(steps > 1e-8) or np.all(steps < -1e-8))


def _rails_filled(x0, x1, h, l, c, upper, lower, touch_lim, xu, xl):
    """Each rail touched in two time-thirds of the pattern, and used after that.

    The median is taken from that rail's first touch, not from the other side's
    first pivot. Bars before the touch are the empty extension we stop drawing;
    counting them made every triangle look empty. 0.48: a channel or triangle
    that price actually travels has its median bar nearer the rail than mid-range.
    An empty rail stays above that.
    """
    span = x1 - x0
    if span <= 0:
        return False
    thirds_lo, thirds_hi = set(), set()
    for t in range(x0, x1 + 1):
        if not bar_sane(h, l, c, t):
            continue
        w = upper(t) - lower(t)
        if w <= 0:
            return False
        band = touch_lim
        third = min(2, int(3 * (t - x0) / span))
        if float(l[t]) - lower(t) <= band:
            thirds_lo.add(third)
        if upper(t) - float(h[t]) <= band:
            thirds_hi.add(third)
    if len(thirds_lo) < 2 or len(thirds_hi) < 2:
        return False

    def med(t0, gap):
        vals = []
        for t in range(max(int(t0), x0), x1 + 1):
            if not bar_sane(h, l, c, t):
                continue
            w = upper(t) - lower(t)
            if w <= 0:
                return 1.0
            vals.append(gap(t) / w)
        return float(np.median(vals)) if vals else 1.0

    if med(xl, lambda t: float(l[t]) - lower(t)) > 0.48:
        return False
    if med(xu, lambda t: upper(t) - float(h[t])) > 0.48:
        return False
    return True


def _third_left_behind(x0, x1, h, l, c, upper, lower, xu, xl, room):
    """True when some third of a rail never comes back within ``room``.

    Width-fraction gaps still pass when the pattern is very wide, so a line
    can sit far above the highs (Q). After a rail's first touch, every
    time-third has to reach it in dollars.
    """
    span = x1 - x0
    if span <= 0 or room <= 0:
        return False
    best_hi = [None, None, None]
    best_lo = [None, None, None]
    for t in range(x0, x1 + 1):
        if not bar_sane(h, l, c, t):
            continue
        third = min(2, int(3 * (t - x0) / span))
        if t >= int(xu):
            gap = max(0.0, float(upper(t) - h[t]))
            best_hi[third] = gap if best_hi[third] is None else min(best_hi[third], gap)
        if t >= int(xl):
            gap = max(0.0, float(l[t] - lower(t)))
            best_lo[third] = gap if best_lo[third] is None else min(best_lo[third], gap)
    return any(best is not None and best > room for best in best_hi + best_lo)


def _split_anchors(hx, hy, lx, ly, span):
    """True when the first touches are far apart and the earlier one is not a shelf.

    A flat side can start before the other side shows up. A low that is never
    revisited, well before the first high, is the rally trend (ULTA), not a base.
    """
    if span <= 0 or len(hx) == 0 or len(lx) == 0:
        return False
    if abs(int(min(hx)) - int(min(lx))) <= 0.40 * span:
        return False
    if int(min(hx)) < int(min(lx)):
        xs, ys = hx, hy
    else:
        xs, ys = lx, ly
    first_i = int(np.argmin(np.asarray(xs, float)))
    y0 = float(ys[first_i])
    x0 = int(xs[first_i])
    later = [float(y) for x, y in zip(xs, ys) if int(x) != x0]
    if not later:
        return True
    scale = max(abs(y0), 1.0)
    return min(abs(y - y0) for y in later) > 0.03 * scale


def _mid_crosses(x0, x1, c, upper, lower):
    prev = 0
    n = 0
    for t in range(x0, x1 + 1):
        side = 1 if c[t] >= 0.5 * (upper(t) + lower(t)) else -1
        if prev and side != prev:
            n += 1
        prev = side
    return n


def detect(
    h, l, c, a, P,
    flat=0.04, max_w=120, min_w=15, sym_min_w=16, wait=30,
    touch_atr=0.60, min_height_atr=3.0, kinds=None,
    wedge_min_w=16, channel_min_w=20, rect_min_w=25,
):
    I, Pr, H, C = P
    out = []
    n_piv = len(I)
    for k in range(4, n_piv):
        ai = a[C[k]]
        if not np.isfinite(ai) or ai <= 0:
            continue
        s = k - 4
        while s > 0 and I[k] - I[s - 1] <= max_w:
            s -= 1
        best = None
        for start in range(s, k - 3):
            # The backward walk above always keeps the 5-pivot minimum, so that
            # window can be wider than max_w. Enforce the cap here.
            if not (min_w <= I[k] - I[start] <= max_w):
                continue
            cand = _one(
                h, l, c, I, Pr, H, C, start, k, ai, flat, sym_min_w, wait,
                touch_atr, min_height_atr, kinds, wedge_min_w, channel_min_w, rect_min_w,
            )
            # Earliest breakout wins, then the longest pair (so a wedge's wide
            # start cannot outrank it), then score. Ranking by length alone let a
            # candidate that only breaks out later displace one that already had,
            # so a live scan and a full-history scan disagreed (look-ahead).
            if cand is not None and (
                best is None
                or (-cand["signal"], len(cand["points"]), cand["score"])
                > (-best["signal"], len(best["points"]), best["score"])
            ):
                best = cand
        if best is not None:
            out.append(best)
    return out


def _one(
    h, l, c, I, Pr, H, C, start, k, ai, flat, sym_min_w, wait, touch_atr,
    min_height_atr, kinds, wedge_min_w, channel_min_w, rect_min_w,
):
    ih = [i for i in range(start, k + 1) if H[i]]
    il = [i for i in range(start, k + 1) if not H[i]]
    if len(ih) < 2 or len(il) < 2 or len(ih) + len(il) < 5 or max(len(ih), len(il)) < 3:
        return None
    hx = _touch_x(I[ih], Pr[ih], h)
    lx = _touch_x(I[il], Pr[il], l)
    up = _fit_side(hx, Pr[ih], ai, touch_atr, True)
    dn = _fit_side(lx, Pr[il], ai, touch_atr, False)
    if up is None or dn is None:
        return None
    su, bu, ux = up
    sl, bl, dx = dn

    def upper(j, su=su, bu=bu, ux=ux):
        return su * (j - ux) + bu

    def lower(j, sl=sl, bl=bl, dx=dx):
        return sl * (j - dx) + bl

    x0, x1 = int(I[start]), int(I[k])
    w0, w1 = upper(x0) - lower(x0), upper(x1) - lower(x1)
    if w0 < min_height_atr * ai or w1 <= 0:
        return None
    avg_w = 0.5 * (w0 + w1)
    # A labelled pivot has to sit on its boundary. 12% of the height still
    # left the dot visibly off the line.
    touch_lim = min(touch_atr * ai, 0.08 * avg_w)

    def _on_boundary(xs, ys, line_at, inside_positive):
        for x, y in zip(xs, ys):
            gap = (line_at(int(x)) - float(y)) if inside_positive else (float(y) - line_at(int(x)))
            dust = 1e-6 * max(abs(float(y)), 1.0)
            if gap < -dust or gap > touch_lim:
                return False
        return True

    if not _on_boundary(hx, Pr[ih], upper, True) or not _on_boundary(lx, Pr[il], lower, False):
        return None
    # A single long wick must not be the pivot that draws the boundary.
    # min, not max: a wide ATR was letting a needle set the shelf (BMR, CYTK).
    wick_cap = min(1.0 * ai, 0.15 * avg_w)

    def _fat_wick(xs, prices, upper_side):
        # Measure the bar that first printed the extreme. ZigZag keeps the last
        # bar of a flat print, which has already left the extreme.
        for x, y in zip(xs, prices):
            bar = int(x)
            wick = (float(y) - c[bar]) if upper_side else (c[bar] - float(y))
            if wick > wick_cap:
                return True
        return False

    if _fat_wick(hx, Pr[ih], True) or _fat_wick(lx, Pr[il], False):
        return None
    kind = _kind(su, sl, w0, w1, x1 - x0, flat)
    if kind is None or (kinds is not None and kind not in kinds):
        return None
    span = x1 - x0
    # The first high and the first low have to belong to the same coil.
    # A flat shelf may start earlier; a lone extreme that price never revisits
    # is the rally trend, not one side of the pattern (ULTA).
    if _split_anchors(hx, Pr[ih], lx, Pr[il], span):
        return None
    if kind == "symmetrical_triangle" and span < sym_min_w:
        return None
    if kind in ("falling_wedge", "rising_wedge") and span < wedge_min_w:
        return None
    if kind in ("ascending_channel", "descending_channel") and span < channel_min_w:
        return None
    if kind == "rectangle" and span < rect_min_w:
        return None
    if kind == "ascending_triangle" and _marches(Pr[ih], avg_w):
        return None
    if kind == "descending_triangle" and _marches(Pr[il], avg_w):
        return None
    if not _rails_filled(x0, x1, h, l, c, upper, lower, touch_lim, int(min(hx)), int(min(lx))):
        return None
    price = abs(float(c[x1]))
    room = max(2.0 * float(ai), 0.03 * price)
    if _third_left_behind(x0, x1, h, l, c, upper, lower, int(min(hx)), int(min(lx)), room):
        return None
    # Tag the boundaries. Vertical travel understates a steep channel: a high
    # and the next higher low can both sit on their lines and barely differ in price.
    swings = []
    for i in range(start, k + 1):
        width = upper(I[i]) - lower(I[i])
        if width <= 0:
            return None
        if H[i]:
            swings.append((Pr[i] - lower(I[i])) / width)
        else:
            swings.append((upper(I[i]) - Pr[i]) / width)
    if np.median(swings) < 0.60:
        return None
    # A wick or close through either rail before the signal cancels it (EQBK).
    # No pad: the old one-bar slope allowance is what let that pierce through.
    # Pivot bars are the rails. ZigZag's index can sit one bar off the fitted
    # touch, so that print is not a pierce. Any other bar trading through is.
    own_hi = {int(x) for x in hx} | {int(I[i]) for i in ih}
    own_lo = {int(x) for x in lx} | {int(I[i]) for i in il}
    poke = min(0.50 * ai, 0.12 * avg_w)
    for t in range(x0, int(C[k])):
        if not bar_sane(h, l, c, t):
            continue
        dust = 1e-8 * max(abs(upper(t)), abs(lower(t)), 1.0)
        if c[t] > upper(t) + dust or c[t] < lower(t) - dust:
            return None
        # A low pivot can still pierce resistance, and a high pivot support.
        # Exempt the pivot's own rail only (CWT's low candle crossed both).
        if (t not in own_hi and h[t] > upper(t) + poke) or (t not in own_lo and l[t] < lower(t) - poke):
            return None
    if kind in ("ascending_channel", "descending_channel", "rectangle") and _mid_crosses(x0, x1, c, upper, lower) < 2:
        return None

    end = min(len(c), int(C[k]) + wait)
    apex = limit = None
    if kind in _NARROW:
        den = su - sl
        if den >= -1e-9:
            return None
        apex = (su * ux - bu - sl * dx + bl) / den
        if not np.isfinite(apex) or apex <= x1 + 1:
            return None
        limit = x0 + 0.85 * (apex - x0)
    want = _WANT.get(kind, 0)
    for j in range(int(C[k]), end):
        if not bar_sane(h, l, c, j):
            continue
        if apex is not None and (j >= apex or j > limit):
            break
        u, d_ = upper(j), lower(j)
        if u <= d_:
            break
        # A close on the line is a touch. Clear a slice of the width, not float dust.
        clear = max(0.08 * ai, 0.015 * (u - d_))
        direction = 1 if c[j] > u + clear else -1 if c[j] < d_ - clear else 0
        if not direction:
            # Pivot confirmation starts the wait for a closing breakout; it
            # does not end containment. A prior wick through a rail cancels
            # this pair under the same allowance used above (PRTCY).
            # A candle can include yesterday's close as its opening extreme.
            # Compare that print with yesterday's rail, allowing a natural
            # rebound in a sloping channel without exempting a stale shelf.
            dust = 1e-8 * max(abs(u), abs(d_), 1.0)
            hi_x = j - 1 if j > 0 and abs(h[j] - c[j - 1]) <= dust else j
            lo_x = j - 1 if j > 0 and abs(l[j] - c[j - 1]) <= dust else j
            if h[j] > upper(hi_x) + poke or l[j] < lower(lo_x) - poke:
                return None
            continue
        if want and direction != want:
            return None
        # A cliff bar is not a breakout. A channel that has opened up by the
        # signal is no longer parallel (GDRZF's rail, extended past the pivots).
        if has_split(c, j, j) or float(h[j] - l[j]) > 2.0 * avg_w:
            return None
        if kind in ("ascending_channel", "descending_channel"):
            wj = u - d_
            if wj < 0.85 * w0 or wj > 1.15 * w0:
                continue
        # Each rail starts at its own first touch, not at the other side's pivot.
        xu, xl = int(min(hx)), int(min(lx))
        lines = [(xu, upper(xu), j, u), (xl, lower(xl), j, d_)]
        points = []
        hi_n = lo_n = 0
        hi_i = lo_i = 0
        for i in range(start, k + 1):
            if H[i]:
                hi_n += 1
                points.append((hx[hi_i], Pr[ih[hi_i]], f"high {hi_n}"))
                hi_i += 1
            else:
                lo_n += 1
                points.append((lx[lo_i], Pr[il[lo_i]], f"low {lo_n}"))
                lo_i += 1
        level = u if direction == 1 else d_
        return sig(kind, direction, x0, x1, j, level, len(ih) + len(il) + w0 / ai, lines, points)
    return None
