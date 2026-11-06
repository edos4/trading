"""Double-bottom geometry, trend context, invalidation, and causal timing."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from patterns_v2.patterns import detect
from patterns_v2.patterns import double_bottom
from patterns_v2.patterns.common import atr, zigzag
from patterns_v2.signals import detect_trade_signals


def _bars(
    xs: list[int] | None = None, ys: list[float] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs = xs or [0, 20, 35, 50, 65, 79]
    ys = ys or [100, 140, 90, 110, 91, 115]
    c = np.interp(np.arange(xs[-1] + 1), xs, ys)
    return c + 1, c - 1, c


def _hits(h: np.ndarray, l: np.ndarray, c: np.ndarray, **kwargs) -> list[dict]:
    a = atr(h, l, c)
    return double_bottom.detect(h, l, c, a, zigzag(h, l, c, a), **kwargs)


def _known_pivots(h, l, c, confirmed=68):
    """Control the confirmation clock independently of breakout bars."""
    a = atr(h, l, c)
    idx = np.array([20, 35, 50, 65])
    p = (idx, np.array([h[20], l[35], h[50], l[65]]),
         np.array([True, False, True, False]), np.array([22, 38, 53, confirmed]))
    return a, p


@pytest.mark.parametrize("prior", [115, 120])
def test_prior_high_must_clear_the_neckline_by_ten_percent(prior):
    assert not _hits(*_bars(ys=[100, prior, 90, 110, 91, 115]))


def test_high_is_relative_to_price_and_not_an_absolute_share_price():
    bars = _bars()
    normal = _hits(*bars)
    cheap = _hits(*(x * 0.01 for x in bars))
    expensive = _hits(*(x * 100 for x in bars))
    assert normal and cheap and expensive
    assert normal[0]["signal"] == cheap[0]["signal"] == expensive[0]["signal"]
    assert normal[0]["score"] == pytest.approx(cheap[0]["score"])


@pytest.mark.parametrize("xs,ys", [
    ([0, 30, 31, 46, 61, 75], [100, 140, 90, 110, 91, 117]),
    ([0, 20, 30, 31, 46, 61, 75], [100, 140, 140, 90, 110, 91, 117]),
    ([0, 20, 30, 31, 46, 61, 75], [100, 140, 139, 90, 110, 91, 117]),
])
def test_one_plunge_does_not_establish_a_sustained_downtrend(xs, ys):
    assert not _hits(*_bars(xs, ys))


def test_prior_trend_is_bounded_and_can_be_configured():
    bars = _bars()
    assert _hits(*bars)
    assert not _hits(*bars, trend_lookback=5)
    assert not _hits(*bars, min_trend_bars=20)


def test_small_bounce_in_decline_does_not_replace_the_real_prior_high():
    bars = _bars([0, 20, 28, 32, 40, 55, 70, 84],
                 [100, 150, 111, 119, 90, 110, 91, 117])
    hits = _hits(*bars)
    assert hits
    assert hits[0]["points"][0] == (20, 151, "trend")


def test_minor_swings_inside_the_w_do_not_require_consecutive_pivots():
    bars = _bars([0, 20, 35, 43, 47, 55, 70, 84],
                 [100, 140, 90, 104, 98, 112, 91, 117])
    hits = _hits(*bars)
    assert hits
    assert hits[0]["level"] == 113
    assert hits[0]["start"] == 35
    assert hits[0]["end"] == 70


def test_rounded_lower_second_bottom_is_one_support_test():
    bars = _bars([0, 20, 35, 50, 65, 69, 79],
                 [100, 140, 90, 110, 89, 90, 115])
    assert _hits(*bars)


def test_flat_bottom_prints_belong_to_one_test():
    bars = _bars([0, 20, 35, 39, 52, 67, 71, 85],
                 [100, 140, 90, 90, 110, 91, 91, 117])
    assert _hits(*bars)


def test_narrow_adam_valley_is_not_automatically_a_bad_print():
    h, l, c = _bars()
    # A legitimate long lower wick. OHLC alone cannot prove it is bad data.
    c[35], h[35] = 98, 100
    assert _hits(h, l, c)


@pytest.mark.parametrize("xs,ys", [
    ([0, 20, 35, 50, 65, 72, 80, 94], [100, 140, 90, 110, 91, 105, 90, 117]),
    ([0, 20, 35, 42, 48, 58, 75, 89], [100, 140, 90, 105, 91, 110, 90, 117]),
])
def test_three_distinct_support_tests_are_not_a_double_bottom(xs, ys):
    # This also checks that pairing nonadjacent pivots cannot skip a test.
    h, l, c = _bars(xs, ys)
    # An earlier W may already have confirmed before the third test existed;
    # future bars must not erase it. No new signal may follow the third test.
    assert not [s for s in _hits(h, l, c) if s["signal"] > xs[-2]]


@pytest.mark.parametrize("ys", [
    [100, 140, 90, 110, 96, 115],  # Lows do not match.
    [100, 140, 90, 95, 91, 115],  # No material intervening rally.
])
def test_shallow_or_mismatched_w_is_rejected(ys):
    assert not _hits(*_bars(ys=ys))


def test_neckline_is_the_highest_traded_high_between_lows():
    h, l, c = _bars(ys=[100, 140, 90, 110, 91, 119])
    # A second peak between the lows must become resistance.
    h[55], l[55], c[55] = 115, 108, 109
    hits = _hits(h, l, c)
    assert hits
    assert hits[0]["level"] == 115
    assert c[hits[0]["signal"]] > 115


def test_intraday_neckline_touch_is_not_a_confirmed_break():
    h, l, c = _bars()
    c[66:] = np.minimum(c[66:], 111)
    h[66:] = np.maximum(h[66:], c[66:] + 1)
    l[66:] = c[66:] - 1
    a, p = _known_pivots(h, l, c)
    assert double_bottom.detect(h, l, c, a, p) == []


def test_close_below_floor_cancels_before_a_later_breakout():
    h, l, c = _bars()
    h[70], l[70], c[70] = 89, 87, 88
    a, p = _known_pivots(h, l, c)
    assert double_bottom.detect(h, l, c, a, p) == []


def test_intraday_new_low_invalidates_the_old_confirmed_pivot():
    h, l, c = _bars()
    l[70] = 88
    a, p = _known_pivots(h, l, c)
    assert double_bottom.detect(h, l, c, a, p) == []


def test_breakout_bar_cannot_hide_a_new_lower_low():
    h, l, c = _bars()
    j = _hits(h, l, c)[0]["signal"]
    l[j] = 88
    a, p = _known_pivots(h, l, c)
    assert double_bottom.detect(h, l, c, a, p) == []


def test_negative_hold_cannot_backdate_a_signal():
    with pytest.raises(ValueError, match="hold"):
        _hits(*_bars(), hold=-1)


def test_wait_is_measured_from_second_low_not_late_pivot_confirmation():
    h, l, c = _bars()
    a, p = _known_pivots(h, l, c, confirmed=76)
    # The breakout on 78 is outside ten bars after the low on 65.
    assert double_bottom.detect(h, l, c, a, p, wait=10) == []
    assert double_bottom.detect(h, l, c, a, p, wait=13)


def test_early_break_is_delayed_until_pivot_is_known():
    h, l, c = _bars()
    a, p = _known_pivots(h, l, c, confirmed=79)
    hits = double_bottom.detect(h, l, c, a, p)
    assert hits and hits[0]["signal"] == 79


def test_failed_early_break_cannot_be_reissued_after_pivot_confirmation():
    h, l, c = _bars([0, 20, 35, 50, 65, 70, 73, 79],
                    [100, 140, 90, 110, 91, 115, 106, 117])
    a, p = _known_pivots(h, l, c, confirmed=74)
    assert double_bottom.detect(h, l, c, a, p) == []


def test_hold_confirmation_is_stamped_when_it_becomes_known():
    h, l, c = _bars([0, 20, 35, 50, 65, 79, 84], [100, 140, 90, 110, 91, 115, 118])
    normal = _hits(h, l, c)
    held = _hits(h, l, c, hold=2)
    assert normal and held
    assert held[0]["signal"] == normal[0]["signal"] + 2
    j = held[0]["signal"]
    assert not _hits(h[:j], l[:j], c[:j], hold=2)
    assert _hits(h[:j + 1], l[:j + 1], c[:j + 1], hold=2) == held


def test_new_lower_low_during_hold_invalidates_the_pending_confirmation():
    h, l, c = _bars([0, 20, 35, 50, 65, 79, 84], [100, 140, 90, 110, 91, 115, 118])
    j = _hits(h, l, c)[0]["signal"]
    l[j + 1] = 88
    assert c[j + 1] > 111  # Closing above resistance cannot hide the new low.
    assert not _hits(h, l, c, hold=2)


@pytest.mark.parametrize("bad", ["nan", "flat", "outside"])
def test_bad_print_cannot_confirm_a_breakout(bad):
    h, l, c = _bars()
    j = _hits(h, l, c)[0]["signal"]
    if bad == "nan":
        h[j] = np.nan
    elif bad == "flat":
        h[j] = l[j] = c[j]
    else:
        h[j] = c[j] - 1
    a, p = _known_pivots(h, l, c)
    # A later valid close can confirm, but the invalid bar cannot.
    hits = double_bottom.detect(h, l, c, a, p)
    assert all(s["signal"] != j for s in hits)
    assert hits


@pytest.mark.parametrize("bear", [False, True])
@pytest.mark.parametrize("case", ["plain", "rounded", "subdivided"])
def test_real_pipeline_is_causal_and_mirrors_levels(bear, case):
    if case == "rounded":
        h, l, c = _bars([0, 20, 35, 50, 65, 69, 79], [100, 140, 90, 110, 89, 90, 115])
    elif case == "subdivided":
        h, l, c = _bars([0, 20, 35, 43, 47, 55, 70, 84],
                        [100, 140, 90, 104, 98, 112, 91, 117])
    else:
        h, l, c = _bars()
    if bear:
        h, l, c = 250 - l, 250 - h, 250 - c
    name = "double_top" if bear else "double_bottom"
    full = [s for s in detect(h, l, c) if s["pattern"] == name]
    assert full
    for n in range(60, len(c) + 1):
        prefix = [s for s in detect(h[:n], l[:n], c[:n]) if s["pattern"] == name]
        assert prefix == [s for s in full if s["signal"] < n]
        latest = [s for s in detect(h[:n], l[:n], c[:n], latest=n - 1)
                  if s["pattern"] == name and s["signal"] == n - 1]
        assert latest == [s for s in full if s["signal"] == n - 1]
    raw = full[0]
    n = raw["signal"] + 1
    df = pd.DataFrame({"open": c[:n], "high": h[:n], "low": l[:n],
                       "close": c[:n], "volume": 1000},
                      index=pd.bdate_range("2024-01-02", periods=n))
    trades = detect_trade_signals(df, symbol="TEST", timeframe="1d", latest_bar_only=True)
    trade = next(s for s in trades if s.pattern == name)
    assert trade.action == ("SELL" if bear else "BUY")
    assert trade.signal_bar_idx == n - 1
    if bear:
        assert trade.take_profit < trade.price < trade.stop_loss
    else:
        assert trade.stop_loss < trade.price < trade.take_profit


def test_pinned_histories_match_every_causal_prefix():
    root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache" / "us"
    positives = 0
    for path in sorted(root.glob("*.json")):
        rows = json.loads(path.read_text())["bars"]
        h, l, c = (np.array([row[key] for row in rows], float) for key in ("h", "l", "c"))
        full = _hits(h, l, c)
        positives += len(full)
        for n in range(60, len(c) + 1):
            assert _hits(h[:n], l[:n], c[:n]) == [s for s in full if s["signal"] < n], (path.stem, n)
    assert positives, "Causality checks must exercise real confirmed patterns"
