"""Nonempty default-parameter fixtures and causal replay for all enabled families."""
from __future__ import annotations

import numpy as np
import pytest

from patterns_v2.patterns import PATTERN_NAMES, detect
from patterns_v2.test_double_bottom import _bars


_BOUNDED = {
    "rectangle": (0, 0),
    "ascending_channel": (.10, .10), "descending_channel": (-.10, -.10),
    "ascending_triangle": (0, .18), "descending_triangle": (-.18, 0),
    "symmetrical_triangle": (-.10, .10),
    "falling_wedge": (-.25, -.06), "rising_wedge": (.06, .25),
}
_MIRRORS = {"double_top": "double_bottom", "inv_cup_handle": "cup_handle",
            "bear_flag": "bull_flag", "bear_pennant": "bull_pennant"}
_FAMILIES = set(_BOUNDED) | set(_MIRRORS) | set(_MIRRORS.values())


def _bounded(kind: str):
    su, sl = _BOUNDED[kind]
    xs = np.array([20, 28, 36, 44, 52, 60, 68, 76])
    ys = [100 + sl * (t-20) + 1 if i % 2 == 0 else 125 + su * (t-20) - 1
          for i, t in enumerate(xs)]
    down = kind in {"descending_triangle", "descending_channel", "rising_wedge"}
    end = 100 + sl * 64 - 8 if down else 125 + su * 64 + 8
    c = np.interp(np.arange(95), np.r_[0, xs, 84, 94], np.r_[90, ys, end, end])
    return c + 1, c - 1, c


def _pole(kind: str):
    c = np.full(90, 100.)
    h, l = c + 1, c - 1
    c[40:53] = np.linspace(100, 160, 13)
    h[40:53], l[40:53] = c[40:53] + 1, c[40:53] - 1
    for t in range(53, 63):
        d = t - 53
        h[t] = 159 - (.4 * d if kind == "bull_pennant" else .08 * d)
        l[t] = 149 + (.4 * d if kind == "bull_pennant" else -.08 * d)
        c[t] = (h[t] + l[t]) / 2 + (-1 if d % 2 else 1)
    c[63:], h[63:], l[63:] = 172, 173, 171
    return h, l, c


def _cup():
    c = np.interp(np.arange(100), [0, 20, 30, 40, 50, 70, 78, 83, 99],
                  [50, 100, 80, 75, 77, 100, 94, 106, 110])
    return c + 1, c - 1, c


def family_bars(name: str):
    if name in _MIRRORS:
        h, l, c = family_bars(_MIRRORS[name])
        offset = {"inv_cup_handle": 200, "double_top": 250}.get(name, 300)
        return offset - l, offset - h, offset - c
    if name in _BOUNDED:
        return _bounded(name)
    if name in {"bull_flag", "bull_pennant"}:
        return _pole(name)
    if name == "cup_handle":
        return _cup()
    return _bars()


def test_every_enabled_family_has_a_positive_fixture():
    assert _FAMILIES == set(PATTERN_NAMES)


def test_historical_signals_cannot_precede_the_public_api_warmup():
    from patterns_v2.patterns.bull_flag import detect as flags
    from patterns_v2.patterns.common import atr, zigzag

    h, l, c = (x[25:] for x in _pole("bull_flag"))
    a = atr(h, l, c)
    assert any(s["signal"] < 59 for s in flags(h, l, c, a, zigzag(h, l, c, a)))
    assert not [s for s in detect(h, l, c) if s["signal"] < 59]


@pytest.mark.parametrize("h,l,c", [([1], [0], [1, 2]), ([[1, 2]], [[0, 1]], [[1, 2]])])
def test_public_detector_rejects_misaligned_arrays(h, l, c):
    with pytest.raises(ValueError, match="equal-length one-dimensional"):
        detect(h, l, c)


@pytest.mark.parametrize("name", sorted(_FAMILIES))
def test_default_detectors_have_nonempty_causal_and_latest_equivalent_results(name):
    h, l, c = family_bars(name)
    full = detect(h, l, c)
    assert any(s["pattern"] == name for s in full), name
    assert all(s["signal"] >= 59 for s in full), "No signals before the public API's warmup"
    for n in range(60, len(c) + 1):
        prefix = detect(h[:n], l[:n], c[:n])
        assert prefix == [s for s in full if s["signal"] < n], (name, n)
        current = [s for s in detect(h[:n], l[:n], c[:n], latest=n-1) if s["signal"] == n-1]
        assert current == [s for s in full if s["signal"] == n-1], (name, n)


@pytest.mark.parametrize("bear", [False, True])
def test_cup_handle_is_frozen_at_breakout_even_if_future_handle_window_has_a_lower_low(bear):
    h, l, c = family_bars("inv_cup_handle" if bear else "cup_handle")
    name = "inv_cup_handle" if bear else "cup_handle"
    original = next(s for s in detect(h, l, c) if s["pattern"] == name)
    j = original["signal"]
    c[j+2] = 108 if bear else 92
    h[j+2], l[j+2] = c[j+2] + 1, c[j+2] - 1
    prefix = detect(h[:j+1], l[:j+1], c[:j+1])
    appended = detect(h, l, c)
    assert original in prefix
    assert original in appended
