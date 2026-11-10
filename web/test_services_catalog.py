"""Explorer charts survive a pinned pattern catalog the engine refuses.

Drawing a symbol does not depend on pattern resolution, so a catalog the engine
will not execute (detector-interface drift, an empty catalog) is reported with
the chart as ``scan_error`` instead of failing the request. Only an editor
database outage stays fatal — web/app.py maps it to 503.
"""

from __future__ import annotations

from unittest.mock import Mock

import pandas as pd
import pytest

from data.tv_client import OHLCVCandle
from web.services import ExplorerService

REFUSAL = "Version runtime dependency is incompatible: patterns/base_pattern.py"


def _candles(count: int = 300):
    closes = [100.0 + (index % 7) for index in range(count)]
    return [OHLCVCandle(c, c + 1, c - 1, c, 1000, stamp.to_pydatetime())
            for c, stamp in zip(closes, pd.bdate_range("2024-01-01", periods=count))]


def _service(monkeypatch, candles):
    monkeypatch.setattr("web.services.fetch_ohlcv_candles", lambda *a, **kw: candles)
    service = ExplorerService()
    service._renderer.render = Mock(return_value=b"png")
    return service


def test_unrunnable_pinned_catalog_still_returns_the_chart(monkeypatch):
    from core.pattern_edit_store import EditError

    service = _service(monkeypatch, _candles())
    monkeypatch.setattr("web.services.discover_patterns",
                        Mock(side_effect=EditError(REFUSAL)))
    result = service.load_symbol("TEST", "NASDAQ", "1d", volume_gate=False)
    assert result["signals"] == []
    assert result["scan_error"] == REFUSAL
    assert result["chart_png_b64"]  # the chart is rendered regardless
    assert result["bars"] > 0


def test_editor_database_outage_is_still_fatal(monkeypatch):
    from core.pattern_editor_db import DatabaseUnavailable

    service = _service(monkeypatch, _candles())
    monkeypatch.setattr("web.services.discover_patterns", Mock(
        side_effect=DatabaseUnavailable("PostgreSQL unavailable or operation timed out")))
    with pytest.raises(DatabaseUnavailable):
        service.load_symbol("TEST", "NASDAQ", "1d", volume_gate=False)


def test_indicators_only_never_touches_the_catalog(monkeypatch):
    service = _service(monkeypatch, _candles())
    monkeypatch.setattr("web.services.discover_patterns",
                        Mock(side_effect=AssertionError("patterns")))
    monkeypatch.setattr("web.services.latest_signals_over_lookback", lambda *a, **kw: [])
    result = service.load_symbol("TEST", "NASDAQ", "1d", indicators_only=True,
                                 volume_gate=False)
    assert result["scan"] == "indicators"
    assert result["scan_error"] is None
