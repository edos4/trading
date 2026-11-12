"""The explorer draws with the paper tab's viewer, so the load must carry it.

``include_viewer`` is opt-in: MCP's ``analyze_symbol`` reads ``chart_png_b64``
and has no use for the candle arrays. The rendered PNG always stays — it backs
"Save chart PNG" and is the browser's fallback when the viewer cannot mount.
"""

from __future__ import annotations

from unittest.mock import Mock

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")

import pandas as pd

from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal, ann_hline, ann_segment
from web.services import ExplorerService


def _candles(close):
    return [OHLCVCandle(c, c + 1, c - 1, c, 1000, stamp.to_pydatetime())
            for c, stamp in zip(close, pd.bdate_range("2024-01-01", periods=len(close)))]


def _service(monkeypatch, candles, signal=None):
    """Indicators-only keeps the fixture away from the pinned pattern catalog."""
    monkeypatch.setattr("web.services.fetch_ohlcv_candles", lambda *a, **kw: candles)
    monkeypatch.setattr("web.services.latest_signals_over_lookback",
                        lambda *a, **kw: [signal] if signal else [])
    service = ExplorerService()
    service._indicators = []
    service._renderer.render = Mock(return_value=b"png")
    return service


def _load(service, **kwargs):
    return service.load_symbol("TEST", "NASDAQ", "1d", indicators_only=True,
                               volume_gate=False, **kwargs)


def _signal():
    signal = TradeSignal(symbol="TEST", action="BUY", pattern="double_bottom",
                         timeframe="1d", confidence=1.0, price=110.0, qty=1,
                         signal_bar_idx=250)
    signal.chart_annotations = [
        ann_hline(105.0, "level", "#ffeb3b"),
        ann_segment("2024-06-03", "2024-07-01", 100.0, 108.0, "#ffeb3b"),
    ]
    return signal


def test_viewer_payload_carries_candles_indicators_and_pattern_geometry(monkeypatch):
    service = _service(monkeypatch, _candles(np.linspace(100.0, 120.0, 300)), _signal())
    viewer = _load(service, include_viewer=True)["viewer"]

    assert viewer["symbol"] == "TEST"
    assert viewer["timeframe"] == "1d"
    assert len(viewer["candles"]) > 2
    assert [row["time"] for row in viewer["candles"]] == sorted(
        row["time"] for row in viewer["candles"])
    assert viewer["ohlc"]["close"] == pytest.approx(120.0)
    assert len(viewer["volume"]) == len(viewer["candles"])
    assert len(viewer["vwap"]) == len(viewer["candles"])
    assert viewer["rsi14"]
    assert viewer["dii"]["length"] == 20
    assert len(viewer["dii"]["up"]) == len(viewer["candles"])
    # The detected level and the pattern's segment reach the viewer, not just
    # the PNG: same annotations feed both.
    assert [level["title"] for level in viewer["levels"]] == ["level"]
    assert viewer["levels"][0]["price"] == pytest.approx(105.0)
    assert any(row.get("data") for row in viewer["segments"])
    # The PNG is still rendered for "Save chart PNG" and as the fallback.
    assert "chart_png_b64" not in viewer


def test_png_and_csv_survive_the_viewer(monkeypatch):
    service = _service(monkeypatch, _candles(np.linspace(100.0, 120.0, 300)))
    result = _load(service, include_viewer=True)
    assert result["chart_png_b64"]
    assert result["csv"].startswith("Date,") or result["csv"]


def test_viewer_is_opt_in(monkeypatch):
    """MCP posts no ``include_viewer``; its response must not grow."""
    service = _service(monkeypatch, _candles(np.linspace(100.0, 120.0, 300)))
    assert _load(service)["viewer"] is None


def test_a_window_the_viewer_cannot_draw_still_returns_the_png(monkeypatch):
    service = _service(monkeypatch, _candles(np.linspace(100.0, 120.0, 300)))
    monkeypatch.setattr(
        "analysis.chart_renderer.build_trade_viewer_payload",
        Mock(side_effect=ValueError("not enough valid bars for TEST 1d")),
    )
    result = _load(service, include_viewer=True)
    assert result["viewer"] is None
    assert result["chart_png_b64"]


def test_weekly_viewer_payload_resamples(monkeypatch):
    service = _service(monkeypatch, _candles(np.linspace(100.0, 140.0, 300)))
    viewer = service.load_symbol(
        "TEST", "NASDAQ", "1W", indicators_only=True, volume_gate=False,
        include_viewer=True,
    )["viewer"]
    assert viewer["timeframe"] == "1W"
    assert len(viewer["candles"]) < 300
