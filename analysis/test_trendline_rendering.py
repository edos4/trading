"""Automatic lines use the same candles and coordinates in PNG and viewers."""

import matplotlib.pyplot as plt
from matplotlib.collections import PathCollection
from matplotlib.colors import to_rgba_array
import mplfinance as mpf
import numpy as np
import pandas as pd
import pytest
from copy import deepcopy

from analysis.chart_renderer import ChartRenderer, _nearest_viewer_time, build_trade_viewer_payload
from analysis.trendlines import TRENDLINE_COLOR
from patterns.base_pattern import ANN_PATTERN


def _frame(n=300):
    x = np.arange(n)
    close = 100 + x * .12 + np.sin(x / 3) * 3
    return pd.DataFrame({
        "open": close - .3, "high": close + .8, "low": close - .8,
        "close": close, "volume": 1000.,
    }, index=pd.bdate_range("2024-01-02", periods=n))


@pytest.mark.parametrize("title_case", [False, True])
def test_png_and_viewer_share_sorted_visible_candles_and_rail_geometry(monkeypatch, title_case):
    df = _frame()
    # Duplicate intraday rows must collapse to their latest session candle.
    duplicate = df.iloc[[85]].copy()
    duplicate.index += pd.Timedelta(hours=4)
    df = pd.concat([df, duplicate]).iloc[::-1]
    if title_case:
        df.columns = df.columns.str.title()
    captured = {}

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        captured["df"] = frame
        captured["lines"] = [
            (list(line.get_xdata()), list(line.get_ydata()), line.get_linestyle())
            for line in fig.axes[0].lines if line.get_color() == TRENDLINE_COLOR
        ]
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, trendlines=True)
    payload = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)
    times = [candle["time"] for candle in payload["candles"]]

    assert len(times) == 252
    assert captured["df"].index.strftime("%Y-%m-%d").tolist() == times
    assert payload["trendlines"]
    assert len(captured["lines"]) == len(payload["trendlines"])
    for (xs, ys, style), rail in zip(captured["lines"], payload["trendlines"]):
        assert xs == [times.index(point["time"]) for point in rail["data"]]
        assert ys == pytest.approx([point["value"] for point in rail["data"]])
        assert style == rail["style"]


def test_png_and_viewer_drop_invalid_bars_before_fitting(monkeypatch):
    df = _frame(100)
    df.loc[df.index[12:28], "open"] = np.nan
    df.loc[df.index[40], "high"] = np.inf
    invalid_date = df.iloc[[0]].copy()
    invalid_date.index = pd.DatetimeIndex([pd.NaT])
    df = pd.concat([df, invalid_date])
    captured = {}

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        captured["df"] = frame
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, trendlines=True)
    payload = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)

    assert len(payload["candles"]) == 83
    assert captured["df"].index.strftime("%Y-%m-%d").tolist() == [
        candle["time"] for candle in payload["candles"]
    ]
    assert np.isfinite(captured["df"][["Open", "High", "Low", "Close"]]).all().all()


def test_png_preparation_keeps_caller_overlay_on_the_same_candles(monkeypatch):
    df = _frame().iloc[::-1]
    df.columns = df.columns.str.title()
    values = df["Close"] + 2
    original = values.copy()
    overlay = mpf.make_addplot(values, color="orange", secondary_y=False)
    captured = {}

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        captured["expected"] = (frame["Close"] + 2).to_numpy()
        captured["actual"] = next(
            line.get_ydata() for line in fig.axes[0].lines if line.get_color() == "orange"
        )
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, extra_plots=[overlay], trendlines=True)

    np.testing.assert_allclose(captured["actual"], captured["expected"])
    pd.testing.assert_series_equal(overlay["data"], original)


def test_png_preparation_aligns_indicator_bands_and_masks_without_mutating_them(monkeypatch):
    df = _frame().iloc[::-1]
    df.columns = df.columns.str.title()
    lower, upper = (df["Close"] - .5).to_numpy(), (df["Close"] + .5).to_numpy()
    mask = (df["Close"] > 130).to_numpy()
    overlay = mpf.make_addplot(df["Close"], color="orange", secondary_y=False, fill_between=[
        {"y1": lower, "y2": upper, "label": "indicator band"},
        {"y1": 100., "y2": upper, "where": mask, "label": "masked band"},
    ])
    original_lower, original_upper, original_mask = lower.copy(), upper.copy(), mask.copy()
    real_plot = mpf.plot

    def inspect_plot(frame, **kwargs):
        actual_mask = kwargs["addplot"][0]["fill_between"][1]["where"]
        np.testing.assert_array_equal(actual_mask, frame["Close"].to_numpy() > 130)
        return real_plot(frame, **kwargs)

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        band = next(collection for collection in fig.axes[0].collections
                    if collection.get_label() == "indicator band")
        vertices = band.get_paths()[0].vertices
        for position in (0, 50, len(frame) - 1):
            prices = vertices[np.isclose(vertices[:, 0], position), 1]
            assert prices.min() == pytest.approx(frame["Close"].iloc[position] - .5)
            assert prices.max() == pytest.approx(frame["Close"].iloc[position] + .5)
        plt.close(fig)
        return b""

    monkeypatch.setattr(mpf, "plot", inspect_plot)
    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, extra_plots=[overlay], trendlines=True)

    np.testing.assert_array_equal(lower, original_lower)
    np.testing.assert_array_equal(upper, original_upper)
    np.testing.assert_array_equal(mask, original_mask)


def test_png_preparation_aligns_scatter_markers_sizes_and_colors(monkeypatch):
    df = _frame().iloc[::-1]
    df.columns = df.columns.str.title()
    colors = pd.Series(["red", "green"] * 150, index=df.index)
    sizes = pd.Series(np.arange(len(df)) + 5., index=df.index)
    markers = np.array(["o", "x"] * 150)
    overlay = mpf.make_addplot(df["Close"] + 2, type="scatter", color=colors.to_numpy(),
                               markersize=sizes, marker=markers, secondary_y=False)

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        scatter = next(collection for collection in fig.axes[0].collections
                       if isinstance(collection, PathCollection))
        assert len(scatter.get_paths()) == len(frame)
        np.testing.assert_array_equal(scatter.get_sizes(), sizes.loc[frame.index].to_numpy())
        np.testing.assert_array_equal(scatter.get_facecolors(), to_rgba_array(colors.loc[frame.index]))
        assert len(overlay["markersize"]) == len(df)
        assert len(overlay["marker"]) == len(df)
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, extra_plots=[overlay], trendlines=True)


def test_weekly_png_and_viewer_preserve_saved_geometry_on_the_aggregated_candle(monkeypatch):
    df = _frame(100).resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    dates = [(day - pd.Timedelta(days=1)).strftime("%Y-%m-%d") for day in df.index[:3]]
    annotations = [
        {"type": "segment", "start_date": dates[0], "end_date": dates[1],
         "start_price": 95., "end_price": 97., "label": "Saved rail"},
        {"type": "path", "points": [
            {"date": day, "price": price} for day, price in zip(dates, (95., 97., 96.))
        ], "label": "Saved path"},
    ]
    original = deepcopy(annotations)
    captured = []

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        captured.extend((list(line.get_xdata()), list(line.get_ydata()))
                        for line in fig.axes[0].lines if line.get_color() == ANN_PATTERN)
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1W", df, annotations=annotations, trendlines=True)
    payload = build_trade_viewer_payload(df, symbol="TEST", timeframe="1W", annotations=annotations)

    assert captured == [([0, 1], [95., 97.]), ([0, 1, 2], [95., 97., 96.])]
    assert [point["time"] for point in payload["segments"][1]["data"]] == [
        day.strftime("%Y-%m-%d") for day in df.index[:3]
    ]
    assert annotations == original


def test_weekly_event_mapping_rejects_missing_or_ambiguous_buckets():
    index = pd.DatetimeIndex(["2024-01-01", "2024-01-05", "2024-01-12"])
    assert _nearest_viewer_time(index, "2024-01-04", timeframe="1W") is None
    assert _nearest_viewer_time(index, "2023-12-28", timeframe="1W") is None
    assert _nearest_viewer_time(index, "2024-01-05", timeframe="1W") == "2024-01-05"
    assert _nearest_viewer_time(index, "2024-01-08", timeframe="1W") == "2024-01-12"


@pytest.mark.parametrize("broken", [False, True])
def test_png_paints_blue_rails_at_the_fitted_candle_coordinates(monkeypatch, broken):
    x = np.arange(40)
    close = 100 + x * .25 + np.sin(x / 2) * 3
    df = pd.DataFrame({
        "open": close - .4, "high": close + .6, "low": close - .6,
        "close": close, "volume": 1000.,
    }, index=pd.bdate_range("2024-01-02", periods=40))
    if broken:
        df.loc[df.index[-1], ["open", "high", "low", "close"]] += 114. - close[-1]
    payload = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)
    times = [row["time"] for row in payload["candles"]]
    assert payload["trendlines"]
    assert any(rail["broken"] for rail in payload["trendlines"]) == broken

    def inspect_figure(self, fig, buf, symbol, timeframe, frame, indicators=None):
        fig.canvas.draw()
        pixels = np.asarray(fig.canvas.buffer_rgba())
        # Candles, grid and background cannot satisfy this blue color test.
        rgb = pixels[:, :, :3].astype(float)
        blue = (rgb[:, :, 2] > 150) & (rgb[:, :, 2] > rgb[:, :, 0] * 1.5) & (
            rgb[:, :, 2] > rgb[:, :, 1] * 1.1
        )
        height, width = blue.shape
        for rail in payload["trendlines"]:
            start, end = rail["data"]
            positions = np.linspace(times.index(start["time"]), times.index(end["time"]), 160)
            prices = np.linspace(start["value"], end["value"], 160)
            coordinates = fig.axes[0].transData.transform(np.column_stack((positions, prices)))
            hits = []
            for px, py in coordinates:
                col, row = round(px), height - 1 - round(py)
                hits.append(blue[max(0, row - 2):min(height, row + 3),
                                 max(0, col - 2):min(width, col + 3)].any())
            # Dashed/dotted rails have deliberate gaps; a substantial portion
            # of the fitted segment must still actually paint blue pixels.
            assert np.mean(hits) > .25
        plt.close(fig)
        return b""

    monkeypatch.setattr(ChartRenderer, "_save_figure", inspect_figure)
    ChartRenderer().render("TEST", "1d", df, trendlines=True)
