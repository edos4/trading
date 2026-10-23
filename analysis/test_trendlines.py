from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from analysis.chart_renderer import _trendline_note, build_trade_viewer_payload
from analysis.trendlines import (
    MAX_BODY_PENETRATION_RATIO,
    MAX_WICK_PENETRATION_RATIO,
    TRENDLINE_COLOR,
    _atr,
    _evaluate,
    _pivots,
    _select_line,
    detect_trendlines,
)


def _uptrend_frame(periods: int = 60) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=periods)
    wave = np.sin(np.arange(periods) / 2.0) * 3.0
    close = pd.Series(100 + np.arange(periods) * 0.25 + wave, index=idx, dtype=float)
    return pd.DataFrame({
        "open": close - 0.4,
        "high": close + 0.6,
        "low": close - 0.6,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)


def test_detect_trendlines_returns_blue_support_and_resistance() -> None:
    lines = detect_trendlines(_uptrend_frame())

    assert {line["kind"] for line in lines} == {"support", "resistance"}
    assert all(line["color"] == TRENDLINE_COLOR for line in lines)
    assert all(0 <= line["start_index"] < line["end_index"] <= 59 for line in lines)
    # The synthetic series rises, so both fitted lines rise with it.
    assert all(line["slope"] > 0 for line in lines)


@pytest.mark.parametrize("high", [False, True])
def test_a_flat_extreme_counts_as_one_pivot(high: bool) -> None:
    # The two plateaus are separate swings; each long plateau is only one touch.
    values = np.array([0., 0., 5., 5., 5., 5., 5., 0., 0., 5., 5., 5., 5., 0., 0.])
    if not high:
        values = -values

    points = _pivots(values, left=2, right=2, high=high)

    assert points == [(2, 5. if high else -5.), (9, 5. if high else -5.)]


def test_flat_prices_do_not_falsely_confirm_trendlines() -> None:
    df = _uptrend_frame()
    df[["open", "high", "low", "close"]] = 100.

    assert detect_trendlines(df) == []


@pytest.mark.parametrize("high", [False, True])
@pytest.mark.parametrize("direction", [-1, 1])
def test_flat_steps_in_a_monotonic_trend_are_not_swings(high: bool, direction: int) -> None:
    values = np.repeat(np.arange(8, dtype=float), 10) * direction

    assert _pivots(values, left=3, right=3, high=high) == []


def test_flat_valleys_are_not_high_pivots_and_flat_peaks_are_not_low_pivots() -> None:
    values = np.repeat([100., 102., 100., 102., 100.], 10)

    assert _pivots(values, left=3, right=3, high=True) == [(10, 102.), (30, 102.)]
    assert _pivots(values, left=3, right=3, high=False) == [(20, 100.)]


def test_a_plateau_waits_for_confirmation_after_its_final_bar() -> None:
    unfinished = np.array([0., 0., 5., 5., 5., 5., 5., 5.])

    assert _pivots(unfinished, left=2, right=2, high=True) == []
    assert _pivots(np.r_[unfinished, 0., 0.], left=2, right=2, high=True) == [(2, 5.)]


def test_a_missing_bar_cannot_confirm_a_swing() -> None:
    values = np.array([0., np.nan, 5., 0., 0.])

    assert _pivots(values, left=2, right=2, high=True) == []


@pytest.mark.parametrize("resistance", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_sloping_rails_use_the_clean_edge_of_each_confirmed_plateau(resistance: bool, reverse: bool) -> None:
    lows = np.r_[
        np.full(10, 102.), np.full(11, 100.), np.linspace(104., 114., 19),
        np.full(11, 110.), np.linspace(114., 124., 19), np.full(11, 120.),
        np.linspace(125., 129., 9),
    ]
    df = pd.DataFrame({"open": lows + 1., "high": lows + 2., "low": lows, "close": lows + 1.5})
    if reverse:
        df = df.iloc[::-1].reset_index(drop=True)
    if resistance:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]
    kind = "resistance" if resistance else "support"

    line = next(line for line in detect_trendlines(df) if line["kind"] == kind)

    # The reflected series exercises rising resistance and falling resistance;
    # reversing time exercises both slope directions for each kind of rail.
    assert (line["start_index"], line["end_index"]) == ((9, 69) if reverse else (20, 80))
    assert line["pivots"] == 3  # A flat run contributes one touch, not two edges.
    assert line["confirmed"]
    assert line["r2"] == pytest.approx(1.)
    assert line["body_penetration"] == 0.
    indices = np.arange(line["start_index"], line["end_index"] + 1)
    projected = line["start_price"] + (indices - indices[0]) * line["slope"]
    prices = df["high" if resistance else "low"].to_numpy()[indices]
    assert np.all(prices <= projected + 1e-12) if resistance else np.all(prices >= projected - 1e-12)


@pytest.mark.parametrize("resistance", [False, True])
def test_horizontal_plateaus_still_contribute_one_touch_each(resistance: bool) -> None:
    lows = np.r_[
        np.full(10, 102.), np.full(11, 100.), np.full(19, 102.),
        np.full(11, 100.), np.full(19, 102.), np.full(11, 100.), np.full(9, 102.),
    ]
    df = pd.DataFrame({"open": lows + 1., "high": lows + 2., "low": lows, "close": lows + 1.5})
    if resistance:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]

    line = next(line for line in detect_trendlines(df)
                if line["kind"] == ("resistance" if resistance else "support"))

    assert (line["start_index"], line["end_index"]) == (10, 80)
    assert line["pivots"] == 3
    assert line["confirmed"]
    assert line["slope"] == 0.


@pytest.mark.parametrize("resistance", [False, True])
def test_horizontal_plateau_recency_uses_its_final_confirmed_bar(resistance: bool) -> None:
    lows = np.full(100, 102.)
    lows[10:21], lows[50:81], lows[81:] = 100., 100., 103.
    df = pd.DataFrame({"open": lows + .25, "high": lows + 1., "low": lows, "close": lows + .75})
    if resistance:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]

    line = next(line for line in detect_trendlines(df)
                if line["kind"] == ("resistance" if resistance else "support"))

    # The final plateau began 49 bars ago but its confirmed final touch is
    # only 19 bars old. It must not be dropped as an unextendable stale rail.
    assert (line["start_index"], line["end_index"]) == (10, 80)
    assert line["stale_bars"] == 19
    assert line["pivots"] == 2
    assert not line["confirmed"]


@pytest.mark.parametrize("resistance", [False, True])
def test_horizontal_origin_preserves_span_between_unequal_length_plateaus(resistance: bool) -> None:
    lows = np.full(60, 102.)
    lows[10:42], lows[45:47], lows[47:] = 100., 100., 103.
    df = pd.DataFrame({"open": lows + .25, "high": lows + 1., "low": lows, "close": lows + .75})
    if resistance:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]

    line = next(line for line in detect_trendlines(df)
                if line["kind"] == ("resistance" if resistance else "support"))

    # The final edges are only five bars apart. Starting at the first flat
    # bar preserves the valid 36-bar rail instead of rejecting its span.
    assert (line["start_index"], line["end_index"]) == (10, 46)
    assert line["pivots"] == 2
    assert not line["confirmed"]
    assert line["body_penetration"] == 0.


@pytest.mark.parametrize("resistance", [False, True])
def test_recent_refit_keeps_a_horizontal_plateau_crossing_its_window(resistance: bool) -> None:
    lows = np.full(400, 101.)
    lows[250:] = 111.
    lows[np.arange(10, 231, 20)] = 100.
    lows[270:331], lows[360:366] = 110., 110.
    df = pd.DataFrame({"open": lows + 1., "high": lows + 2., "low": lows, "close": lows + 1.5})
    if resistance:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]

    line = next(line for line in detect_trendlines(df)
                if line["kind"] == ("resistance" if resistance else "support"))

    # The many older touches win the initial search but cannot extend. The
    # recent plateau starts before bar 280 and stays flat through bar 330, so
    # its horizontal origin can be clipped to the recent window and refit.
    assert (line["start_index"], line["end_index"]) == (280, 399)
    assert line["start_price"] == (140. if resistance else 110.)
    assert line["slope"] == 0.
    assert line["pivots"] == 2
    assert not line["confirmed"]
    assert line["body_penetration"] == 0.


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("all_rows", [False, True])
def test_incomplete_ohlc_cannot_silently_bypass_validation(column: str, value: float, all_rows: bool) -> None:
    df = _uptrend_frame()
    if all_rows:
        df[column] = value
    else:
        df.iloc[-1, df.columns.get_loc(column)] = value

    # Missing body/close data must not become zero penetrations or a guessed
    # current price. In particular, all-NaN closes must not emit a warning.
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert detect_trendlines(df) == []


@pytest.mark.parametrize("column,bound,offset", [
    ("open", "high", 1.), ("close", "low", -1.),
    ("high", "low", -1.), ("low", "high", 1.),
])
def test_inconsistent_ohlc_cannot_anchor_rails(column: str, bound: str, offset: float) -> None:
    df = _uptrend_frame()
    df.iloc[-1, df.columns.get_loc(column)] = df[bound].iloc[-1] + offset

    assert detect_trendlines(df) == []


def test_volume_is_not_required_to_validate_price_rails() -> None:
    df = _uptrend_frame()
    expected = detect_trendlines(df)
    df["volume"] = np.nan

    assert expected
    assert detect_trendlines(df) == expected


@pytest.mark.parametrize("tolerance", [np.nan, np.inf, -np.inf, -1.])
def test_tolerance_cannot_disable_comparison_guards(tolerance: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        detect_trendlines(_uptrend_frame(), tolerance=tolerance)


def test_explicit_pivot_windows_override_the_default_length_guard() -> None:
    df = _uptrend_frame(40)

    expected = detect_trendlines(df, left=1, right=1)

    assert expected
    assert detect_trendlines(df, lookback=20, left=1, right=1) == expected


@pytest.mark.parametrize("options", [{"left": 0}, {"right": -1}, {"lookback": 0}])
def test_pivot_windows_must_include_bars_on_both_sides(options: dict) -> None:
    with pytest.raises(ValueError, match="positive"):
        detect_trendlines(_uptrend_frame(), **options)


@pytest.mark.parametrize("rising", [False, True])
@pytest.mark.parametrize("breaks_rail", [False, True])
def test_broken_state_compares_price_at_the_same_bar(rising: bool, breaks_rail: bool) -> None:
    df = _uptrend_frame(40)
    if breaks_rail:
        # A close beyond the projected resistance is a real break, even though
        # the displayed segment still ends at its last confirmed pivot.
        delta = 114. - df["close"].iloc[-1]
        for name in ("open", "high", "low", "close"):
            df.iloc[-1, df.columns.get_loc(name)] += delta
    if not rising:
        original = df.copy()
        for name in ("open", "close"):
            df[name] = 220. - original[name]
        df["high"] = 220. - original["low"]
        df["low"] = 220. - original["high"]
    kind = "resistance" if rising else "support"

    line = next(line for line in detect_trendlines(df) if line["kind"] == kind)

    assert line["end_index"] < len(df) - 1
    current_line_price = line["start_price"] + line["slope"] * (len(df) - 1 - line["start_index"])
    current_close = df["close"].iloc[-1]
    if rising:
        assert line["end_price"] < current_close
        assert (current_close > current_line_price) == breaks_rail
    else:
        assert line["end_price"] > current_close
        assert (current_close < current_line_price) == breaks_rail
    assert line["broken"] is breaks_rail


@pytest.mark.parametrize("offset", [0, 100])
def test_stale_refit_can_choose_a_lower_scoring_current_rail(offset: int) -> None:
    n = 100 + offset
    # Many old touches win the initial score, but their rail can no longer be
    # extended near the current close. Two new touches still form a usable rail.
    points = [(i + offset, 100.) for i in range(10, 71, 10)]
    points += [(80 + offset, 110.), (90 + offset, 110.)]
    bars = np.full(n, 101.)
    bars[75 + offset:] = 111.
    bodies = bars + 1.
    for index, price in points:
        bars[index] = price

    selected = _select_line(
        points, bars, bodies, False,
        tol=.4, n=n, min_span=6, last_close=112., end_tolerance=112. * .12, atr=2.,
    )

    assert selected is not None
    line, touches, end = selected
    assert line["p0"] == 110.
    assert touches == [80 + offset, 90 + offset]
    assert end == 90 + offset


@pytest.mark.parametrize("high", [False, True])
@pytest.mark.parametrize("body_cuts", [False, True])
def test_later_clean_bars_do_not_hide_cuts_in_the_drawn_anchor_span(high: bool, body_cuts: bool) -> None:
    n = 40
    points = [(10, 100.), (20, 100.)]
    bars = np.full(n, 101.)
    bodies = np.full(n, 102.)
    bars[[10, 20]] = 100.
    # The short displayed rail spans 11 bars. Two body cuts exceed its 10%
    # limit, and three wick cuts exceed its 20% limit. Both pass when divided
    # by all 30 bars from the first anchor to the end of the input instead.
    cuts = slice(15, 17 if body_cuts else 18)
    bars[cuts] = 99.
    if body_cuts:
        bodies[cuts] = 99.2
    if high:
        bars, bodies = 200. - bars, 200. - bodies

    assert _select_line(
        points, bars, bodies, high,
        tol=.4, n=n, min_span=6, last_close=98. if high else 102., end_tolerance=12., atr=2.,
    ) is None


@pytest.mark.parametrize("high", [False, True])
@pytest.mark.parametrize("penetrating_wicks", [False, True])
def test_a_stale_rail_only_extends_across_clean_wicks(high: bool, penetrating_wicks: bool) -> None:
    n = 200
    points = [(10, 100.), (100, 100.), (170, 100.)]
    bars = np.full(n, 101.)
    bodies = np.full(n, 101.5)
    bars[[10, 100, 170]] = 100.
    if penetrating_wicks:
        # Every wick after the last touch is on the wrong side. Their 15%
        # share of the full history must not hide the 100% extension failure.
        bars[171:] = 99.
    if high:
        bars, bodies = 200. - bars, 200. - bodies

    selected = _select_line(
        points, bars, bodies, high,
        tol=.4, n=n, min_span=16, last_close=98.5 if high else 101.5, end_tolerance=12., atr=2.,
    )

    if penetrating_wicks:
        assert selected is None
    else:
        assert selected is not None
        assert selected[2] == n - 1


def test_seeded_ohlc_rails_preserve_geometry_and_price_scale() -> None:
    # Use binary fractions so these stock-split-like changes in price units
    # introduce no rounding differences or changes in the underlying swings.
    for seed in range(20):
        rng = np.random.default_rng(seed)
        close = 100. + np.cumsum(rng.integers(-4, 5, size=120)) / 4.
        opens = close + rng.integers(-2, 3, size=120) / 4.
        df = pd.DataFrame({
            "open": opens,
            "high": np.maximum(opens, close) + rng.integers(1, 5, size=120) / 4.,
            "low": np.minimum(opens, close) - rng.integers(1, 5, size=120) / 4.,
            "close": close,
        })
        lines = detect_trendlines(df)
        tolerance = max(_atr(df) * .4, np.median(close) * .002)
        for line in lines:
            start, end = line["start_index"], line["end_index"]
            assert 0 <= start < end < len(df)
            assert line["pivots"] >= 2
            assert line["confirmed"] == (line["pivots"] >= 3)
            projected = line["start_price"] + np.arange(end - start + 1) * line["slope"]
            assert line["end_price"] == pytest.approx(projected[-1])
            if line["kind"] == "support":
                bars = df["low"].to_numpy()[start:end + 1]
                bodies = np.minimum(opens, close)[start:end + 1]
                wick_cuts = bars < projected - tolerance
                body_cuts = bodies < projected - tolerance
            else:
                bars = df["high"].to_numpy()[start:end + 1]
                bodies = np.maximum(opens, close)[start:end + 1]
                wick_cuts = bars > projected + tolerance
                body_cuts = bodies > projected + tolerance
            assert line["start_price"] == bars[0]
            assert np.mean(wick_cuts) <= MAX_WICK_PENETRATION_RATIO
            assert np.mean(body_cuts) <= MAX_BODY_PENETRATION_RATIO
            assert line["body_penetration"] == pytest.approx(np.mean(body_cuts))
        for factor in (2. ** -20, 2. ** 20):
            scaled = detect_trendlines(df * factor)
            assert len(scaled) == len(lines)
            for original, changed in zip(lines, scaled):
                for field in ("kind", "start_index", "end_index", "pivots", "broken", "confirmed"):
                    assert changed[field] == original[field]
                for field in ("start_price", "end_price", "slope"):
                    assert changed[field] == pytest.approx(original[field] * factor)


def test_pivots_before_a_line_start_do_not_count_as_violations() -> None:
    # A recent support rail must not be punished by lows from an older regime.
    points = [(5, 10.0), (20, 55.0), (30, 54.0), (40, 56.0)]
    bars = np.full(50, 60.0)
    result = _evaluate(
        points, 20, 55.0, 30, 54.0, bars, bars, False,
        tol=1.0, n=50, last_close=54.0, end_tolerance=100.0, atr=1.0,
    )

    assert result is not None
    assert result["violations"] == 0


def test_body_penetration_rejects_a_rail_cutting_candles() -> None:
    points = [(20, 55.0), (30, 54.0)]
    bars = np.full(50, 60.0)    # wicks sit above the support rail
    bodies = np.full(50, 50.0)  # candle bodies cut through it
    result = _evaluate(
        points, 20, 55.0, 30, 54.0, bars, bodies, False,
        tol=1.0, n=50, last_close=54.0, end_tolerance=100.0, atr=1.0,
    )

    assert result is None


def test_stale_rails_are_refit_or_dropped() -> None:
    periods = 200
    idx = pd.bdate_range("2024-01-02", periods=periods)
    values = np.concatenate([
        np.linspace(100, 130, 150),
        130 + np.sin(np.arange(50) / 2.0) * 1.2,
    ])
    close = pd.Series(values, index=idx)
    df = pd.DataFrame({
        "open": close - 0.3,
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)

    lines = detect_trendlines(df)

    assert lines
    assert all(line["stale_bars"] <= 20 for line in lines)


@pytest.mark.parametrize("periods", [1640, 2000])
@pytest.mark.parametrize("falling", [False, True])
def test_long_trade_history_can_fit_a_clean_recent_regime(periods: int, falling: bool) -> None:
    recent = 100 + np.arange(150) * .25 + np.sin(np.arange(150) / 2) * 3
    close = np.r_[np.linspace(10., 100., periods - 150), recent]
    df = pd.DataFrame({
        "open": close - .4, "high": close + .6, "low": close - .6,
        "close": close, "volume": 1000.,
    }, index=pd.bdate_range("2015-01-02", periods=periods))
    if falling:
        original = df.copy()
        df["open"], df["close"] = 250. - original["open"], 250. - original["close"]
        df["high"], df["low"] = 250. - original["low"], 250. - original["high"]

    # Keeping an old open trade in view must not make the full-window minimum
    # span exceed the entire recent refit window and suppress every rail.
    payload = build_trade_viewer_payload(df, symbol="TEST", entry_time=df.index[20], trendlines=True)

    assert len(payload["candles"]) == periods
    assert {line["kind"] for line in payload["trendlines"]} == {"support", "resistance"}
    assert all(line["data"][0]["time"] >= df.index[-150].date().isoformat()
               for line in payload["trendlines"])


def test_resistance_ignores_an_isolated_spike() -> None:
    periods = 160
    idx = pd.bdate_range("2024-01-02", periods=periods)
    close = pd.Series(100 + np.arange(periods) * 0.15 + np.sin(np.arange(periods) / 3) * 2, index=idx)
    df = pd.DataFrame({
        "open": close - 0.3,
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)
    # One blow-off spike far above the broad regime must not anchor the rail.
    df.iloc[70, df.columns.get_loc("high")] = 160.0

    lines = detect_trendlines(df)
    resistance = next(line for line in lines if line["kind"] == "resistance")

    assert resistance["end_price"] < 140
    assert resistance["pivots"] >= 2


def test_single_unconfirmed_rail_is_not_called_confirmed() -> None:
    note = _trendline_note([{
        "kind": "support", "confirmed": False, "broken": False,
        "pivots": 2, "channel": "single",
    }])

    assert note == (
        "Only the support rail is present — unconfirmed (2 touches); dotted."
    )
    assert "is confirmed" not in note


def test_single_broken_rail_reports_dashed() -> None:
    note = _trendline_note([{
        "kind": "resistance", "confirmed": True, "broken": True,
        "pivots": 4, "channel": "single",
    }])

    assert note == "Only the resistance rail is present — broken; dashed."


def test_pair_of_unconfirmed_rails_is_explained() -> None:
    note = _trendline_note([
        {"kind": "support", "confirmed": False, "broken": False,
         "pivots": 2, "channel": "converging"},
        {"kind": "resistance", "confirmed": False, "broken": False,
         "pivots": 2, "channel": "converging"},
    ])

    assert note == (
        "Rail state: support unconfirmed (2 touches); "
        "resistance unconfirmed (2 touches)."
    )


def test_confirmed_pair_needs_no_note() -> None:
    note = _trendline_note([
        {"kind": "support", "confirmed": True, "broken": False,
         "pivots": 4, "channel": "converging"},
        {"kind": "resistance", "confirmed": True, "broken": False,
         "pivots": 4, "channel": "converging"},
    ])

    assert note == ""


def test_viewer_payload_adds_trendlines_only_when_requested() -> None:
    df = _uptrend_frame()

    plain = build_trade_viewer_payload(df, symbol="TEST")
    charted = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)

    assert plain["trendlines"] == []
    assert plain["trendline_note"] == ""
    assert len(charted["trendlines"]) == 2
    first = charted["trendlines"][0]
    assert first["color"] == TRENDLINE_COLOR
    assert first["style"] in ("-", "--", ":")
    assert isinstance(charted["trendline_note"], str)
    candle_times = {row["time"] for row in charted["candles"]}
    assert first["data"][0]["time"] in candle_times
    assert first["data"][0]["time"] < first["data"][-1]["time"]
    assert all(np.isfinite(point["value"]) for point in first["data"])


def test_viewer_payload_explains_when_no_rail_is_available() -> None:
    idx = pd.bdate_range("2024-01-02", periods=5)
    close = pd.Series([10.0, 10.2, 10.1, 10.3, 10.2], index=idx)
    df = pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)

    payload = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)

    assert payload["trendlines"] == []
    assert "No confirmed support/resistance" in payload["trendline_note"]
