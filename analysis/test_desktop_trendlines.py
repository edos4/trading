"""The desktop viewer consumes the automatic rails in shared chart payloads."""

from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pandas as pd
import pytest

from ui.tv_chart import TradingViewChart


def _candles(n=40):
    return [
        {"time": str(day.date()), "open": 115., "high": 120., "low": 110., "close": 115.}
        for day in pd.bdate_range("2024-01-02", periods=n)
    ]


@pytest.mark.parametrize("style,dash", [("-", ()), ("--", (6, 4)), (":", (1, 3))])
def test_desktop_automatic_rail_is_clipped_styled_and_included_in_scale(style, dash):
    chart = object.__new__(TradingViewChart)
    chart._candles = _candles()
    chart._start, chart._visible = 10, 20
    chart._levels, chart._volume, chart._forecast = [], {}, {}
    chart._hover = None
    chart._canvas = Mock()
    chart._canvas.winfo_width.return_value = 500
    chart._canvas.winfo_height.return_value = 400
    chart._forecast_color = "yellow"
    rail = {
        "color": "#42a5f5", "width": 3, "style": style,
        "data": [{"time": chart._candles[0]["time"], "value": 10.},
                 {"time": chart._candles[-1]["time"], "value": 400.}],
    }
    chart._payload = {"trendlines": [rail]}
    # Keep the actual viewport calculation and overlay renderer; other panes
    # do not affect whether the rail is visible or the price range it needs.
    for method in ("_draw_grid", "_draw_candles", "_polyline", "_draw_volume",
                   "_draw_rsi", "_draw_levels", "_draw_axis", "_draw_markers", "_draw_drawings"):
        setattr(chart, method, Mock())

    chart._redraw()

    assert list(chart._visible_pattern_segments(chart._candles[10:30])) == [
        (rail, [(0, 110.), (19, 300.)]),
    ]
    # Only the clipped portion affects autoscale, not the offscreen endpoints.
    assert chart._price_lo == pytest.approx(87.2)
    assert chart._price_hi == pytest.approx(322.8)
    chart._canvas.create_line.assert_called_once()
    call = chart._canvas.create_line.call_args
    assert call.kwargs == {"fill": "#42a5f5", "width": 3, "dash": dash}
    assert call.args == pytest.approx((chart._x_for(0), chart._y_price(110.),
                                     chart._x_for(19), chart._y_price(300.)))


def test_desktop_rail_note_and_full_initial_window_survive_payload_loading():
    class Variable:
        def __init__(self, value=""):
            self.value = value

        def set(self, value):
            self.value = value

        def get(self):
            return self.value

    chart = object.__new__(TradingViewChart)
    chart._read_only = True
    chart._title_var = Variable()
    chart._default_legend = "RSI 14"
    chart._legend_var = Variable("RSI 14")
    chart._legend_label = Mock()
    chart._set_ohlc_label = Mock()
    chart._redraw = Mock()
    payload = {
        "symbol": "TEST", "candles": _candles(220),
        "trendlines": [{"kind": "support", "data": []}],
        "trendline_note": "Only the support rail is present — unconfirmed (2 touches); dotted.",
    }

    chart.set_payload(payload)

    assert chart._start == 0 and chart._visible == 220
    assert payload["trendline_note"] in chart._legend_var.get()
    assert "Auto rails: solid confirmed" in chart._legend_var.get()
    assert chart.correction_snapshot()["manual_corrections"] == []


def test_detection_chart_enables_rails_using_only_the_detection_tape():
    from core.pattern_editor_api import PatternEditor
    from data.barcache import load

    # Real frozen tapes carry exchange-open UTC timestamps, spanning daylight
    # saving time. Their rendered market dates must match the saved detection.
    tape = load("us", "CDNS", root=Path(__file__).parents[1] / "tests/fixtures/barcache")
    rows = [[c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume]
            for c in tape[:120]]
    event = {"status": "detected", "symbol": "CDNS", "pattern": "test_pattern",
             "pattern_version_id": "test-version", "session": rows[99][0],
             "action": "BUY", "entry": rows[99][4]}
    settings = SimpleNamespace(market="us", timeframe="1d")
    inputs = SimpleNamespace(request=SimpleNamespace(preset=SimpleNamespace(settings=settings)),
                             dataset="frozen-tape")
    editor = object.__new__(PatternEditor)
    editor.backtests = Mock()
    editor.backtests.result.return_value = {"signals": {"events": [event]}, "inputs": inputs}
    editor.backtests._read_json.return_value = {"CDNS": rows}

    payload = editor.run_chart("test-run", 0)

    assert payload["candles"][0]["time"] == "2025-06-20"
    assert payload["candles"][-1]["time"] == "2025-11-10"
    assert len(payload["candles"]) == 100
    assert any(m["time"] == "2025-11-10" and m["text"] == "BUY" for m in payload["markers"])
    assert payload["trendlines"]
    assert all(point["time"] <= "2025-11-10"
               for rail in payload["trendlines"] for point in rail["data"])


def _interactive_chart():
    chart = object.__new__(TradingViewChart)
    chart._candles = _candles(100)
    chart._start, chart._visible = 20, 60
    chart._levels, chart._volume, chart._forecast = [], {}, {}
    chart._hover = None
    chart._forecast_color = "yellow"
    chart._canvas = Mock()
    chart._canvas.winfo_width.return_value = 600
    chart._canvas.winfo_height.return_value = 400
    rail = {"color": "#42a5f5", "data": [
        {"time": chart._candles[0]["time"], "value": 100.},
        {"time": chart._candles[-1]["time"], "value": 199.},
    ]}
    chart._payload = {"trendlines": [rail]}
    for method in ("_draw_grid", "_draw_candles", "_polyline", "_draw_volume",
                   "_draw_rsi", "_draw_levels", "_draw_axis", "_draw_markers", "_draw_drawings"):
        setattr(chart, method, Mock())
    chart._redraw()
    return chart, rail


@pytest.mark.parametrize("fraction,factor", [(.25, .5), (.5, .5), (.75, .5), (.5, 1.25)])
def test_desktop_zoom_keeps_candle_and_rail_under_cursor(fraction, factor):
    chart, rail = _interactive_chart()
    x = chart._plot[0] + fraction * (chart._plot[2] - chart._plot[0])
    absolute = chart._start + fraction * chart._visible

    chart._zoom_at(x, factor)

    # The window uses integer bar positions, so at most half a bar of rounding
    # is possible. Both candles and rail interpolation use the same positions.
    assert abs(chart._start + fraction * chart._visible - absolute) <= .5
    visible = chart._candles[chart._start:chart._start + chart._visible]
    assert list(chart._visible_pattern_segments(visible)) == [(rail, [
        (0, 100. + chart._start),
        (len(visible) - 1, 100. + chart._start + len(visible) - 1),
    ])]


def test_desktop_pan_and_resize_reclip_the_rail_inside_price_pane():
    chart, rail = _interactive_chart()
    chart._pan(10)
    visible = chart._candles[30:90]
    expected = [(rail, [(0, 130.), (59, 189.)])]
    assert list(chart._visible_pattern_segments(visible)) == expected
    old_coords = chart._canvas.create_line.call_args.args

    chart._canvas.winfo_width.return_value = 900
    chart._canvas.winfo_height.return_value = 650
    chart._redraw()

    assert list(chart._visible_pattern_segments(visible)) == expected
    coords = chart._canvas.create_line.call_args.args
    assert coords != old_coords
    x0, y0, x1, y1 = chart._plot
    assert all(x0 < x < x1 and y0 < y < y1 for x, y in zip(coords[::2], coords[1::2]))


def test_desktop_automatic_rail_stops_at_actual_history_before_forecast():
    chart, rail = _interactive_chart()
    forecasts = [{**row, "predicted": True} for row in _candles(120)[100:]]
    chart._candles.extend(forecasts)
    chart._start, chart._visible = 80, 40
    chart._forecast = {row["time"]: 160. for row in forecasts}

    chart._redraw()

    visible = chart._candles[80:120]
    assert list(chart._visible_pattern_segments(visible)) == [(rail, [(0, 180.), (19, 199.)])]
    assert chart._canvas.create_line.call_args.args[-2] == chart._x_for(19)
    assert chart._x_for(19) < chart._x_for(20)  # the first predicted candle


def test_weekly_anchor_mapping_preserves_labels_and_rejects_ambiguous_or_missing_week():
    from analysis.chart_renderer import _weekly_chart_time

    # Providers can label an existing weekly candle Monday or Friday. Match
    # its week without moving the candle or selecting a neighboring week.
    monday = pd.DatetimeIndex(["2024-01-01", "2024-01-08", "2024-01-15"])
    original = monday.copy()
    assert _weekly_chart_time(monday, "2024-01-11", "America/New_York") == "2024-01-08"
    assert monday.equals(original)
    assert _weekly_chart_time(monday, "2023-12-28", "America/New_York") is None
    ambiguous = pd.DatetimeIndex(["2024-01-08", "2024-01-12"])
    assert _weekly_chart_time(ambiguous, "2024-01-11", "America/New_York") is None
    assert _weekly_chart_time(ambiguous, "2024-01-08", "America/New_York") == "2024-01-08"


def test_legacy_weekly_rounding_marker_is_aligned_before_geometry_recovery():
    from analysis.chart_renderer import build_trade_viewer_payload

    idx = pd.date_range("2021-01-01", periods=160, freq="W-FRI")
    close = [80. + (i - 70) ** 2 * .01 for i in range(160)]
    df = pd.DataFrame({
        "open": [c - .2 for c in close], "high": [c + 1 for c in close],
        "low": [c - 1 for c in close], "close": close, "volume": 1000,
    }, index=idx)
    marker = {"type": "marker", "date": (idx[70] - pd.Timedelta(days=1)).date().isoformat(),
              "price": close[70], "label": "bottom"}
    original = dict(marker)

    weekly = build_trade_viewer_payload(df, symbol="TEST", timeframe="1W",
                                        pattern="pattern_004_rounding_bottom", annotations=[marker])

    curve = next(s for s in weekly["segments"] if s["label"] == "Rounding bottom fit")
    assert [point["time"] for point in curve["data"]] == [d.date().isoformat() for d in idx[10:131]]
    assert [point["value"] for point in curve["data"]] == pytest.approx(close[10:131])
    assert marker == original
    # Same-week aggregation is exclusive to weekly charts. Missing daily
    # pivots retain the existing strict rule rather than snapping to Friday.
    daily = build_trade_viewer_payload(df, symbol="TEST", timeframe="1d",
                                       pattern="pattern_004_rounding_bottom", annotations=[marker])
    assert daily["segments"] == [] and daily["markers"] == []
