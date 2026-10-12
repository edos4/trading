"""Double bottom (W), Bulkowski identification guidelines.

Two valleys after a decline from a higher base, within 3% of each other, with
at least a 10% rise between them. The swing high before the pattern has to
clear the neck, and price has to have been falling into the first valley.
Confirmed by a close above the peak between the valleys. A close below the
lower valley before that cancels it. Bearish twin is the mirror: a double top
has to rise from a lower base.
"""
import numpy as np

from .common import bar_sane, first_break, has_split, sig


def detect(
    h, l, c, a, P,
    tol_pct=0.03, peak_pct=0.10, trend_pct=0.10, base_pct=0.10,
    min_w=10, max_w=120, wait=40, hold=0,
):
    I, Pr, H, C = P
    out = []
    for k in range(3, len(I)):
        # prior high, low 1, neck, low 2
        if not (H[k - 3] and not H[k - 2] and H[k - 1] and not H[k]):
            continue
        ai = a[C[k]]
        if not np.isfinite(ai) or ai <= 0:
            continue
        if not (min_w <= I[k] - I[k - 2] <= max_w):
            continue
        prior, lo1, neck, lo2 = Pr[k - 3], Pr[k - 2], Pr[k - 1], Pr[k]
        ref = abs((lo1 + lo2) / 2.0)
        if ref <= 0 or not np.isfinite(ref):
            continue
        if abs(lo2 - lo1) / ref > tol_pct:
            continue
        # Rally between the valleys, measured off the higher valley.
        if (neck - max(lo1, lo2)) / ref < peak_pct:
            continue
        # Fell into the pattern from a higher base. The prior swing has to
        # clear the neck, not sit inside the same range as the W, and the
        # leg into the first valley has to be a decline.
        if abs(lo1) <= 0 or abs(neck) <= 0:
            continue
        if (prior - lo1) / abs(lo1) < trend_pct or prior <= neck:
            continue
        if (prior - neck) / abs(neck) < base_pct:
            continue
        i_prior, i_lo1, i_neck, i_lo2 = (int(I[k - 3]), int(I[k - 2]), int(I[k - 1]), int(I[k]))
        if not (i_prior < i_lo1 < i_neck < i_lo2 and i_neck > i_lo1 + 1 and i_lo2 > i_neck + 1):
            continue
        floor = min(lo1, lo2)
        height = neck - floor
        if height <= 0:
            continue
        # Close near the extreme, and the neck bar closed near the neck.
        # On the mirror this is the peak close and the trough close (ASA, FUNI).
        if c[i_lo1] - lo1 > 0.30 * height or c[i_lo2] - lo2 > 0.30 * height:
            continue
        if neck - c[i_neck] > 0.30 * height:
            continue
        # The neck is a separate swing. A valley whose high reaches into the
        # neck is the same bar, not a W (the horizontal used to start on that bar).
        if h[i_lo1] > neck - 0.08 * height or h[i_lo2] > neck - 0.08 * height:
            continue
        # A print back at either extreme in the middle is a third test.
        # The ends are excluded: the second extreme is within tol_pct of the
        # first, so the bars that form it are not another test.
        span = i_lo2 - i_lo1
        band = tol_pct * ref
        if any(
            abs(float(l[t]) - lo1) <= band or abs(float(l[t]) - lo2) <= band
            for t in range(i_lo1 + span // 3, i_lo2 - span // 3)
        ):
            continue

        def _spike(idx, vs_move):
            if idx <= 0 or idx >= len(h) - 1:
                return False
            rng = float(h[idx] - l[idx])
            nbr = max(float(h[idx - 1] - l[idx - 1]), float(h[idx + 1] - l[idx + 1]))
            return nbr > 0 and rng > 2.5 * nbr and rng > vs_move

        # Trend and valleys are swings, not one-bar spikes (CACI).
        if _spike(i_prior, 0.5 * abs(float(prior - lo1))) or _spike(i_lo1, 0.40 * height) or _spike(i_lo2, 0.40 * height):
            continue
        j = first_break(c, C[k], C[k] + wait, lambda _: neck, floor=floor, margin=0.05 * height, h=h, l=l)
        if j is None or has_split(c, i_prior, j) or float(h[j] - l[j]) > 2.0 * height:
            continue
        # A later print back at either extreme is another test (NP, TALMF).
        # Measured from the shallower extreme: the 3% price band is wider than
        # a normal pullback once the two tops already sit inside it (META).
        shallow = max(float(lo1), float(lo2))
        dust = 1e-8 * max(abs(shallow), 1.0)
        if any(
            bar_sane(h, l, c, t) and float(l[t]) <= shallow + dust
            for t in range(i_lo2 + 1, j)
        ):
            continue
        if hold and (j + hold >= len(c) or not (c[j + 1 : j + 1 + hold] > neck).all()):
            continue
        lines = [
            (I[k - 3], prior, I[k - 2], lo1),
            (I[k - 2], lo1, I[k - 1], neck),
            (I[k - 1], neck, I[k], lo2),
            (I[k - 1], neck, j, neck),
        ]
        points = [
            (I[k - 3], prior, "trend"),
            (I[k - 2], lo1, "low 1"),
            (I[k - 1], neck, "neck"),
            (I[k], lo2, "low 2"),
        ]
        out.append(sig("double_bottom", 1, I[k - 2], I[k], j, neck, (neck - floor) / ai, lines, points))
    return out
