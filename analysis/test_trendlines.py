from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.chart_renderer import _trendline_note, build_trade_viewer_payload
from analysis.trendlines import TRENDLINE_COLOR, _evaluate, detect_trendlines


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
