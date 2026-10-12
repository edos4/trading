"""
Chart-pattern detection on daily OHLCV.

Each named pattern lives in its own module under ``patterns/``. Bullish-reversal
detectors are mirrored to bearish ones by running on inverted prices.
"""
import re

import numpy as np

from . import (
    ascending_channel,
    ascending_triangle,
    bull_flag,
    bull_pennant,
    cup_handle,
    descending_channel,
    double_bottom,
    falling_wedge,
    inv_head_shoulders,
    rectangle,
    rising_wedge,
)
from ._triangle_base import detect as detect_triangles
from .common import DISABLED, MIRROR, PATTERN_NAMES, atr, first_break, sig, tradable, zigzag


_SWAP = {"low": "high", "high": "low", "bottom": "top", "top": "bottom"}
_SWAP_RE = re.compile(r"\b(low|high|bottom|top)\b")


def _mirror_label(label):
    """Swap high/low and top/bottom in both directions ("pole top" -> "pole bottom")."""
    return _SWAP_RE.sub(lambda m: _SWAP[m.group(1)], label)


def _mirror_signal(s):
    s["pattern"] = MIRROR[s["pattern"]]
    s["direction"] = -1
    s["level"] = -s["level"]
    s["lines"] = [(x0, -y0, x1, -y1) for x0, y0, x1, y1 in s["lines"]]
    s["points"] = [(x, -y, _mirror_label(label)) for x, y, label in s["points"]]
    return s


_BULLISH_DETECTORS = (
    double_bottom.detect,
    bull_flag.detect,
    bull_pennant.detect,
    cup_handle.detect,
)


def detect(h, l, c, zz_mult=2.0):
    """h, l, c: float numpy arrays for ONE symbol, sorted by date. Returns list of signal dicts."""
    h, l, c = (np.asarray(x, float) for x in (h, l, c))
    if len(c) < 60:
        return []
    a = atr(h, l, c)
    sigs = []
    for d, (hh, ll, cc) in ((1, (h, l, c)), (-1, (-l, -h, -c))):
        P = zigzag(hh, ll, cc, a, zz_mult)
        if d == 1:
            bullish_pivots = P
        found = []
        for fn in _BULLISH_DETECTORS:
            found.extend(fn(hh, ll, cc, a, P))
        # Bearish head-and-shoulders confirms on the joining neckline. The
        # up-sloping inverse keeps the horizontal at the right armpit.
        shoulder_name = "inv_head_shoulders" if d == 1 else "head_shoulders"
        if shoulder_name not in DISABLED:
            found.extend(inv_head_shoulders.detect(hh, ll, cc, a, P, join_neck=(d == -1)))
        for s in found:
            if d == -1:
                _mirror_signal(s)
            sigs.append(s)
    sigs += detect_triangles(h, l, c, a, bullish_pivots)
    sigs = [s for s in sigs if s["pattern"] not in DISABLED and tradable(h, l, c, s["start"], s["signal"])]
    seen, uniq = set(), []
    for s in sorted(sigs, key=lambda s: (s["signal"], -s["score"])):
        key = (s["pattern"], s["signal"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq


def add_forward_returns(sig, o, c, horizons=(5, 10, 20)):
    """Entry = open of bar after the breakout. Returns sign-adjusted forward returns by direction."""
    e = sig["signal"] + 1
    out = {}
    for hz in horizons:
        out[f"fwd_{hz}"] = (c[e + hz - 1] / o[e] - 1) * sig["direction"] if e + hz - 1 < len(c) else np.nan
    return out


__all__ = [
    "MIRROR",
    "PATTERN_NAMES",
    "add_forward_returns",
    "atr",
    "detect",
    "first_break",
    "sig",
    "zigzag",
    "ascending_channel",
    "ascending_triangle",
    "bear_flag",
    "bear_pennant",
    "bull_flag",
    "bull_pennant",
    "cup_handle",
    "descending_channel",
    "descending_triangle",
    "double_bottom",
    "double_top",
    "falling_wedge",
    "head_shoulders",
    "inv_cup_handle",
    "inv_head_shoulders",
    "rectangle",
    "rising_wedge",
    "symmetrical_triangle",
]
