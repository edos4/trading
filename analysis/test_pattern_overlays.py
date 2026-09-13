from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from analysis.chart_renderer import ChartRenderer, build_trade_viewer_payload
from patterns._annotations import pattern_annotations, rounding_annotations
from patterns.base_pattern import ANN_PATTERN, ann_hline, ann_marker, ann_path, ann_segment


def frame(n=160):
    x = np.arange(n)
    close = 80 + (x - 70) ** 2 * 0.01
    return pd.DataFrame({"open": close - .2, "high": close + 1, "low": close - 1,
                         "close": close, "volume": 1000}, index=pd.bdate_range("2024-01-02", periods=n))


@pytest.mark.parametrize("parts", [
    [(10, 110, "H1"), (20, 95, "valley"), (30, 108, "H2"), (40, 94, "entry")],
    [(10, 90, "L1"), (20, 105, "neckline"), (30, 92, "L2"), (40, 106, "entry")],
    [(10, 110, "LS"), (20, 95, "LN"), (30, 120, "HEAD"),
     (40, 97, "RN"), (50, 108, "RS"), (60, 94, "entry")],
])
def test_saved_reversal_pivots_become_yellow_outlines(parts):
    df = frame()
    anns = [ann_marker(str(df.index[i].date()), price, label, "#ef5350") for i, price, label in parts]
    original = deepcopy(anns)
    payload = build_trade_viewer_payload(df, symbol="TEST", annotations=anns)
    assert anns == original
    assert payload["segments"][0]["data"] == [
        {"time": str(df.index[i].date()), "value": price} for i, price, _ in parts
    ]
    assert all(s["color"] == ANN_PATTERN for s in payload["segments"])
    assert all(m["color"] == ANN_PATTERN and m["price"] == price
               for m, (_, price, _) in zip(payload["markers"], parts))
    assert any("First" in m["text"] or "Left shoulder" == m["text"] for m in payload["markers"])
    upgraded = pattern_annotations(anns)
    assert pattern_annotations(upgraded) == upgraded


@pytest.mark.parametrize("direction", ["bottom", "top"])
def test_rounding_curve_uses_actual_detector_fit_and_keeps_window(direction):
    df = frame()
    if direction == "top":
        for column in ("open", "high", "low", "close"):
            df[column] = 200 - df[column]
        df[["high", "low"]] = df[["low", "high"]].to_numpy()
    setup = SimpleNamespace(center=70, start=10, neckline=float(df.close.iloc[10]))
    anns = rounding_annotations(df, setup, direction)
    anns.append(ann_marker(str(df.index[70].date()), float(df.close.iloc[70]), direction, ANN_PATTERN))
    payload = build_trade_viewer_payload(df, symbol="TEST", annotations=anns,
                                        entry_time=df.index[73], exit_time=df.index[75])
    curve = payload["segments"][0]
    assert len(curve["data"]) == 121
    assert curve["label"] == f"Rounding {direction} fit"
    np.testing.assert_allclose([p["value"] for p in curve["data"]], df.close.iloc[10:131])
    assert payload["candles"][-1]["time"] >= str(df.index[130].date())
    legacy = build_trade_viewer_payload(df, symbol="TEST", pattern=f"pattern_004_rounding_{direction}", annotations=anns[-1:])
    assert legacy["segments"][0] == curve


def test_head_shoulders_neckline_matches_horizontal_detector_threshold():
    anns = [ann_marker("2024-01-02", 110, "LS", "red"),
            ann_segment("2024-01-02", "2024-01-12", 94, 96, "orange")]
    line = pattern_annotations(anns)[1]
    assert line["start_price"] == line["end_price"] == 96
    assert line["label"] == "Neckline"


def test_legacy_flag_draws_saved_consolidation_bounds():
    df = frame()
    date = lambda i: str(df.index[i].date())
    low = float(df.low.iloc[66:76].min())
    high = float(df.high.iloc[66:76].max())
    anns = [ann_marker(date(65), 95, "pole high", "red"),
            ann_marker(date(75), low, "flag low", "green"), ann_hline(high, "flag high", "orange")]
    payload = build_trade_viewer_payload(df, symbol="TEST", pattern="pattern_009_flag_pattern", annotations=anns)
    assert {s["label"] for s in payload["segments"]} == {"Flag resistance", "Flag support"}
    for segment in payload["segments"]:
        assert segment["data"][0]["time"] == date(66)
        assert segment["data"][-1]["time"] == date(75)
    assert next(m for m in payload["markers"] if m["text"] == "Flag low")["time"] == date(70)


def test_geometry_does_not_snap_missing_pivots_to_unrelated_candles():
    df = frame()
    payload = build_trade_viewer_payload(df, symbol="TEST", annotations=[
        ann_marker("2000-01-01", 1234, "L1", "red"),
        ann_segment("2000-01-01", "2024-01-12", 1234, 100, "orange"),
        ann_hline(99, "stop", "#ef5350"), ann_hline(120, "target", "#26a69a"),
    ])
    assert payload["markers"] == []
    assert payload["segments"] == []
    assert {l["color"] for l in payload["levels"]} == {"#ef5350", "#26a69a"}


def test_curve_draws_multiple_legs_when_panned_on_desktop_and_png():
    from ui.tv_chart import TradingViewChart
    df = frame(40)
    points = [(str(df.index[i].date()), price) for i, price in [(5, 110), (15, 90), (25, 110)]]
    ann = ann_path(points)
    payload = build_trade_viewer_payload(df, symbol="TEST", annotations=[ann])
    chart = object.__new__(TradingViewChart)
    chart._canvas = Mock()
    chart._candles, chart._payload = payload["candles"], payload
    chart._start, chart._visible = 10, 11
    chart._plot = (0, 0, 110, 100)
    chart._price_lo, chart._price_hi = 90, 110
    chart._draw_pattern_segments(chart._candles[10:21])
    assert chart._canvas.create_line.call_args.args == (5, 50, 55, 100, 105, 50)
    axis = Mock()
    ChartRenderer()._draw_annotations(axis, df, [ann])
    assert list(axis.plot.call_args.args[0]) == [5, 15, 25]
    assert list(axis.plot.call_args.args[1]) == [110, 90, 110]


def test_detected_pennant_saves_both_converging_rails():
    import importlib
    module = importlib.import_module("patterns.010_pennant")
    df = frame(60)
    df.loc[:, ["open", "close"]] = 100.
    df.loc[:, "high"], df.loc[:, "low"] = 101., 99.
    df.loc[df.index[44:50], "close"] = np.linspace(101, 115, 6)
    df.loc[df.index[44:50], "high"] = np.linspace(102, 116, 6)
    df.loc[df.index[44:50], "volume"] = 2000
    df.loc[df.index[50:59], "high"] = np.linspace(116, 114, 9)
    df.loc[df.index[50:59], "low"] = np.linspace(110, 113, 9)
    df.loc[df.index[50:59], "close"] = 113.5
    df.loc[df.index[50:59], "volume"] = 500
    df.loc[df.index[59], ["open", "high", "low", "close", "volume"]] = [116, 118, 115, 117, 2000]
    signal = module.PennantPattern().analyze(SimpleNamespace(symbol="TEST", timeframe="1d"), SimpleNamespace(get_df=lambda *a, **kw: df))
    assert signal is not None
    rails = [a for a in signal.chart_annotations if "pennant fit" in a.get("label", "")]
    assert len(rails) == 2
    upper, lower = rails
    assert upper["end_price"] - lower["end_price"] < upper["start_price"] - lower["start_price"]
    payload = build_trade_viewer_payload(df, symbol="TEST", pattern=signal.pattern, annotations=signal.chart_annotations)
    assert {s["label"] for s in payload["segments"]} == {"Pole", "Upper pennant fit", "Lower pennant fit"}
    assert all(s["color"] == ANN_PATTERN for s in payload["segments"])
