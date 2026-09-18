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


def test_viewer_payload_carries_part_reasons():
    df = frame()
    date = lambda i: str(df.index[i].date())
    payload = build_trade_viewer_payload(
        df, symbol="TEST", pattern="pattern_003_double_bottom",
        annotations=[
            ann_marker(date(10), 95, "L1", "#ffeb3b"),
            ann_marker(date(20), 105, "neckline", "#ffeb3b"),
            ann_marker(date(30), 92, "L2", "#ffeb3b"),
            ann_hline(105, "neckline", "#ffeb3b"),
        ],
    )
    markers = {m["text"]: m for m in payload["markers"]}
    assert "swing low" in markers["First bottom (L1)"]["reason"]
    assert "higher low" in markers["Second bottom (L2)"]["reason"]
    assert markers["Neckline"]["reason"]
    neckline = next(level for level in payload["levels"] if level["title"] == "Neckline")
    assert neckline["reason"]


def test_channel_rails_and_pivots_carry_reasons():
    df = frame(160)
    date = lambda i: str(df.index[i].date())
    payload = build_trade_viewer_payload(
        df, symbol="TEST", pattern="pattern_006_upward_channel",
        annotations=[
            ann_marker(date(5), float(df.high.iloc[5]), "start", "#ffeb3b"),
            ann_marker(date(20), float(df.high.iloc[20]), "SH1", "#ffeb3b"),
            ann_marker(date(30), float(df.low.iloc[30]), "valley", "#ffeb3b"),
            ann_marker(date(45), float(df.high.iloc[45]), "SH2", "#ffeb3b"),
            ann_segment(date(20), date(60), 100, 110, "#ffeb3b"),
            ann_segment(date(30), date(60), 95, 105, "#ffeb3b"),
        ],
    )
    rails = {segment["label"]: segment for segment in payload["segments"]}
    assert "slope" in rails["Upper channel"]["reason"]
    assert "parallel rail" in rails["Lower channel"]["reason"]
    markers = {m["text"]: m for m in payload["markers"]}
    assert "swing high" in markers["First swing high"]["reason"]


def test_png_renderer_prints_the_reason_under_each_part():
    """The explorer renders PNGs without a saved pattern id — infer it."""
    df = frame()
    date = lambda i: str(df.index[i].date())
    axis = Mock()
    ChartRenderer()._draw_annotations(
        axis, df,
        [ann_marker(date(10), 95, "L1", "#ffeb3b"),
         ann_marker(date(30), 92, "L2", "#ffeb3b")],
    )
    captions = [call.args[0] for call in axis.annotate.call_args_list]
    assert any("swing low" in str(text) for text in captions)
    assert any("higher low" in str(text) for text in captions)


def test_desktop_label_prints_the_reason_under_it():
    from ui.tv_chart import TradingViewChart
    chart = object.__new__(TradingViewChart)
    chart._canvas = Mock()
    chart._canvas.bbox.return_value = (0, 0, 80, 12)
    chart._plot = (0, 0, 400, 200)
    chart._pattern_label(100, 60, "Second bottom (L2)", "#ffeb3b", "higher low +7.1%")
    calls = chart._canvas.create_text.call_args_list
    assert [call.kwargs["text"] for call in calls] == [
        "Second bottom (L2)", "higher low +7.1%",
    ]
    note = calls[1].kwargs
    assert note["fill"] == "#787b86"
    assert note["font"] == ("Trebuchet MS", 7)
    assert calls[0].kwargs["font"] == ("Trebuchet MS", 9, "bold")
    assert chart._canvas.create_rectangle.called
    assert chart._canvas.tag_lower.called


def test_desktop_markers_hand_their_reason_to_the_label():
    from ui.tv_chart import TradingViewChart
    chart = object.__new__(TradingViewChart)
    chart._canvas = Mock()
    chart._plot = (0, 0, 400, 200)
    chart._price_lo, chart._price_hi = 0, 100
    chart._visible = 1
    candles = [{"time": "2024-01-02", "low": 90., "high": 100., "close": 95., "open": 94.}]
    chart._markers = {"2024-01-02": [{
        "time": "2024-01-02", "price": 92., "position": "belowBar",
        "color": "#ffeb3b", "shape": "arrowUp", "text": "First bottom (L1)",
        "reason": "swing low, RSI 8",
    }]}
    seen = []
    chart._pattern_label = lambda x, y, text, color, reason="": seen.append((text, reason))
    chart._draw_markers(candles)
    assert seen == [("First bottom (L1)", "swing low, RSI 8")]


def test_desktop_trendline_clips_to_the_visible_window():
    from ui.tv_chart import TradingViewChart
    chart = object.__new__(TradingViewChart)
    chart._start, chart._visible = 10, 20          # bars 10..29 in view
    chart._plot = (0, 0, 400, 200)
    chart._rsi_plot = (0, 300, 400, 400)
    chart._price_lo, chart._price_hi = 0.0, 200.0
    shape = {"kind": "trend", "i1": 0, "p1": 100.0, "i2": 40, "p2": 140.0}
    assert chart._clip_trend(shape) == (0.0, 90.5, 400.0, 70.5)
    assert chart._clip_trend({"kind": "trend", "i1": 0, "p1": 1, "i2": 5, "p2": 6}) is None
    assert chart._clip_trend({"kind": "trend", "i1": 12, "p1": 5, "i2": 12, "p2": 9}) is None


def test_desktop_trendline_can_span_price_and_rsi_panes():
    from ui.tv_chart import TradingViewChart
    chart = object.__new__(TradingViewChart)
    chart._start, chart._visible = 0, 10
    chart._plot = (0, 0, 400, 200)
    chart._rsi_plot = (0, 300, 400, 400)
    chart._price_lo, chart._price_hi = 0.0, 100.0
    # Price end at chart top, RSI end at RSI 100 (top of the RSI panel).
    shape = {"kind": "trend", "pane1": "price", "p1": 100.0,
             "pane2": "rsi", "p2": 100.0, "i1": 0, "i2": 9}
    assert chart._shape_points(shape) == ((20.0, 0.0), (380.0, 300.0))
    assert chart._clip_trend(shape) == (20.0, 0.0, 380.0, 300.0)


def test_desktop_drawings_use_the_pane_under_the_cursor():
    from types import SimpleNamespace
    from ui.tv_chart import TradingViewChart
    chart = object.__new__(TradingViewChart)
    chart._plot = (0, 0, 400, 200)         # price pane
    chart._rsi_plot = (0, 300, 400, 400)   # RSI pane, with a gap between them
    chart._price_lo, chart._price_hi = 0.0, 100.0
    chart._start, chart._visible = 0, 10
    chart._candles = [{"time": str(i)} for i in range(10)]
    chart._draft = None

    assert chart._locate_point(SimpleNamespace(x=200, y=100)) == (5, 50.0, "price")
    assert chart._locate_point(SimpleNamespace(x=200, y=300)) == (5, 100.0, "rsi")
    assert chart._locate_point(SimpleNamespace(x=200, y=250)) is None

    # An RSI-pane trendline maps through the RSI scale, not the price scale.
    chart._canvas = Mock()
    chart._paint_shape(
        {"kind": "trend", "pane1": "rsi", "pane2": "rsi",
         "i1": 0, "p1": 100.0, "i2": 9, "p2": 0.0}, "#42a5f5",
    )
    assert chart._canvas.create_line.call_args.args[1::2] == (300.0, 400.0)
