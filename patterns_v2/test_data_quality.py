"""Invalid bars cannot poison later pivots or count as liquidity."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import lfilter

from patterns_v2.patterns.common import atr, has_split, sane_mask, trade_mask, tradable


def _bars(n=80):
    close = 100 + np.sin(np.arange(n) / 4) * 8
    return close + 1, close - 1, close


@pytest.mark.parametrize("kind", ["nan", "inf", "outside", "inverted", "flat"])
@pytest.mark.parametrize("bad_idx", [5, 35])
def test_atr_ignores_invalid_bars_and_recovers_without_losing_alignment(kind, bad_idx):
    h, l, c = _bars()
    if kind == "nan":
        h[bad_idx] = np.nan
    elif kind == "inf":
        l[bad_idx] = -np.inf
    elif kind == "outside":
        c[bad_idx] = h[bad_idx] * 10
    elif kind == "inverted":
        h[bad_idx], l[bad_idx] = l[bad_idx], h[bad_idx]
    else:
        h[bad_idx] = l[bad_idx] = c[bad_idx]
    good = sane_mask(h, l, c)
    actual = atr(h, l, c)
    expected = atr(h[good], l[good], c[good])
    assert np.isnan(actual[bad_idx])
    np.testing.assert_allclose(actual[good], expected, equal_nan=True)
    assert np.isfinite(actual[-1])
    for n in range(14, len(c) + 1):
        np.testing.assert_allclose(atr(h[:n], l[:n], c[:n]), actual[:n], equal_nan=True)


def test_atr_keeps_existing_values_on_valid_data():
    h, l, c = _bars()
    tr = np.maximum.reduce([h-l, np.abs(h-np.r_[c[0], c[:-1]]), np.abs(l-np.r_[c[0], c[:-1]])])
    alpha = 1 / 14
    expected = lfilter([alpha], [1, -(1-alpha)], tr, zi=[tr[:14].mean() * (1-alpha)])[0]
    expected[:13] = np.nan
    np.testing.assert_array_equal(atr(h, l, c), expected)


def test_empty_atr_is_empty():
    assert atr(np.array([]), np.array([]), np.array([])).size == 0


def test_malformed_bars_do_not_count_as_trades():
    h, l, c = _bars(20)
    h[2] = np.nan
    c[3] = h[3] + 100
    h[4], l[4] = l[4], h[4]
    h[5] = l[5] = c[5]
    np.testing.assert_array_equal(trade_mask(h, l, c), sane_mask(h, l, c))


def test_invalid_wide_bars_cannot_supply_minimum_liquidity():
    h, l, c = _bars(20)
    c[:10] = h[:10] + .01  # Impossible closes without a split-sized jump.
    assert not tradable(h, l, c, 0, 19)


def test_tradability_uses_valid_closes_when_some_bars_are_missing():
    h, l, c = _bars(20)
    h[5] = l[5] = c[5] = np.nan
    assert tradable(h, l, c, 0, 19)


def test_bad_close_cannot_cancel_a_real_double_bottom_as_a_split():
    from patterns_v2.test_double_bottom import _bars, _hits

    h, l, c = _bars()
    c[45] = 1000  # Outside the candle's actual range.
    assert _hits(h, l, c)


def test_split_check_bridges_missing_bars_but_ignores_impossible_closes():
    close = np.array([100., 1000., 101., np.nan, 200.])
    valid = np.array([True, False, True, False, True])
    assert not has_split(close, 0, 2, valid=valid)
    assert has_split(close, 3, 4, valid=valid)
    assert not has_split(np.array([]), 0, 0)
