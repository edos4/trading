"""Detector-level tests: real bars in, real signals out.

`test_patternsv2.py` mocks the raw detection dict, so a detector that silently
returns [] on every chart passes the whole suite. These fixtures feed synthetic
textbook shapes through the actual detectors so an unreachable detector fails
loudly instead.
"""
from __future__ import annotations

import numpy as np
import pytest

from patterns_v2.patterns import _BULLISH_DETECTORS, detect
from patterns_v2.patterns import cup_handle, double_bottom
from patterns_v2.patterns.common import atr, zigzag


def _ohlc(close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return close * 1.01, close * 0.99, close.copy()


def _w_shape() -> np.ndarray:
    """Prior high, 10%+ decline, two lows within 3%, neckline 10%+ above, breakout."""
    px = np.empty(120)
    px[:21] = np.linspace(100, 130, 21)      # prior high, 10%+ over the neck
    px[21:36] = np.linspace(130, 100, 15)    # decline into low 1
    px[36:51] = np.linspace(100, 115, 15)    # neck
    px[51:66] = np.linspace(115, 101, 15)    # low 2, inside 3% of low 1
    px[66:76] = np.linspace(101, 130, 10)    # breakout through the neck
    px[76:] = 130
    return px


def _cup_shape() -> np.ndarray:
    """+40% advance, U cup, handle, then a close through the rim."""
    cup_w, handle_w = 90, 15
    n = 30 + cup_w + handle_w + 30
    px = np.empty(n)
    top = 140.0
    px[:30] = np.linspace(100, top, 30)
    bowl = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(cup_w) / (cup_w - 1))
    px[30 : 30 + cup_w] = top - 30.0 * bowl
    handle_low = top - 30.0 * 0.20
    px[30 + cup_w : 30 + cup_w + handle_w] = np.linspace(top, handle_low, handle_w)
    px[30 + cup_w + handle_w : 30 + cup_w + handle_w + 25] = np.linspace(handle_low, top * 1.06, 25)
    px[30 + cup_w + handle_w + 25 :] = top * 1.06
    return px


def test_double_bottom_fires_on_a_textbook_w():
    h, l, c = _ohlc(_w_shape())
    hits = double_bottom.detect(h, l, c, atr(h, l, c), zigzag(h, l, c, atr(h, l, c), 2.0))
    assert [s["pattern"] for s in hits], "double_bottom cannot see a textbook W"
    s = hits[0]
    assert s["direction"] == 1
    assert s["signal"] > s["start"]


def test_cup_handle_fires_on_a_textbook_cup():
    h, l, c = _ohlc(_cup_shape())
    hits = cup_handle.detect(h, l, c, atr(h, l, c), zigzag(h, l, c, atr(h, l, c), 2.0))
    # The handle is a pause, not a swing: it must not have to be a ZigZag pivot.
    assert hits, "cup_handle cannot see a textbook cup with a 20% handle"


@pytest.mark.parametrize("pattern", ["bull_flag", "bull_pennant", "cup_handle", "double_bottom"])
def test_detectors_return_signal_dicts_and_never_raise(pattern: str):
    from patterns_v2.patterns import MIRROR

    h, l, c = _ohlc(_cup_shape())
    a, P = atr(h, l, c), zigzag(h, l, c, atr(h, l, c), 2.0)
    fn = next(f for f in _BULLISH_DETECTORS if pattern in f.__module__)
    out = fn(h, l, c, a, P)
    assert isinstance(out, list)
    for s in out:
        assert s["pattern"] in {pattern, MIRROR.get(pattern, pattern)}
        assert s["direction"] in (1, -1)
        assert 0 <= s["start"] <= s["signal"] < len(c)


def test_detect_aggregates_without_duplicates():
    h, l, c = _ohlc(_w_shape())
    sigs = detect(h, l, c)
    keys = [(s["pattern"], s["signal"]) for s in sigs]
    assert len(keys) == len(set(keys))
    assert all(0 <= s["signal"] < len(c) for s in sigs)
