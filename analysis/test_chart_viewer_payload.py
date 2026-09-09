from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from analysis.chart_renderer import build_trade_viewer_payload


def test_viewer_payload_has_candles_and_levels() -> None:
    idx = pd.bdate_range("2024-01-02", periods=40)
    close = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame({
        "open": close - 0.4,
        "high": close + 0.8,
        "low": close - 0.9,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)

    payload = build_trade_viewer_payload(
        df,
        symbol="AAPL",
        timeframe="1d",
        pattern="pattern_003_double_bottom",
        action="BUY",
        entry=120.0,
        stop=110.0,
        target=140.0,
        current=139.0,
        entry_time=datetime(2024, 2, 1),
    )
    assert payload["symbol"] == "AAPL"
    assert "chart_png_b64" not in payload
    assert len(payload["candles"]) == 40
    assert payload["candles"][0]["time"] == "2024-01-02"
    assert payload["candles"][-1]["close"] == 139.0
    assert len(payload["volume"]) == 40
    assert "ema20" not in payload
    assert "ema50" not in payload
    assert payload["rsi14"]
    assert payload["rsi14"][-1]["time"] == payload["candles"][-1]["time"]
    assert all(0.0 <= row["value"] <= 100.0 for row in payload["rsi14"])
    titles = {level["title"] for level in payload["levels"]}
    assert titles == {"entry", "stop", "target", "last"}
    assert payload["markers"]
    assert payload["markers"][0]["shape"] == "arrowUp"


def test_viewer_payload_dedupes_duplicate_session_bars() -> None:
    """A tape with two bars per session (04:00 UTC + 13:30 UTC) must not
    produce duplicate/non-monotonic candle times — LightweightCharts rejects
    them and the chart renders blank."""
    # July dates = EDT (UTC-4): both UTC stamps land on the same NY date.
    raw = [
        ("2024-07-01 04:00", 10.0),
        ("2024-07-01 13:30", 10.5),
        ("2024-07-02 04:00", 10.6),
        ("2024-07-02 13:30", 11.0),
        ("2024-07-03 13:30", 11.4),
    ]
    ts = [datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d, _ in raw]
    closes = [c for _, c in raw]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=pd.DatetimeIndex(ts),
    )

    payload = build_trade_viewer_payload(
        df,
        symbol="ALHC",
        timeframe="1d",
        action="SELL",
        entry=11.0,
        stop=12.0,
        target=9.0,
        current=10.5,
        session_tz="America/New_York",
    )
    times = [c["time"] for c in payload["candles"]]
    assert times == ["2024-07-01", "2024-07-02", "2024-07-03"]
    assert len(times) == len(set(times))
    assert all(a < b for a, b in zip(times, times[1:]))
    # The kept bar per session is the later 13:30 UTC one.
    closes_by_time = {c["time"]: c["close"] for c in payload["candles"]}
    assert closes_by_time["2024-07-01"] == 10.5
    assert closes_by_time["2024-07-02"] == 11.0


def test_pattern_geometry_keeps_old_trade_in_view():
    from patterns.base_pattern import ann_segment, ann_marker, ann_hline
    idx = pd.bdate_range("2020-01-01", periods=800)
    df = pd.DataFrame({"open": 100., "high": 110., "low": 90., "close": 101., "volume": 1000}, index=idx)
    payload = build_trade_viewer_payload(
        df, symbol="TEST", action="BUY", entry_time=idx[50], exit_time=idx[60],
        annotations=[
            ann_segment(str(idx[10].date()), str(idx[50].date()), 95, 105, "#ff9800"),
            ann_marker(str(idx[10].date()), 95, "L1", "#26a69a"),
            ann_hline(105, "neckline", "#ff9800"),
        ],
    )
    assert payload["segments"][0]["data"] == [
        {"time": str(idx[10].date()), "value": 95.},
        {"time": str(idx[50].date()), "value": 105.},
    ]
    assert payload["candles"][0]["time"] <= str(idx[10].date())
    assert payload["candles"][-1]["time"] == str(idx[80].date())
    assert any(m["text"] == "L1" for m in payload["markers"])
    assert any(l["title"] == "neckline" for l in payload["levels"])


def test_trade_preserves_annotations_through_ledger_roundtrip():
    from core.backtester import _open_trade
    from core.paper_trader import _trade_to_dict, _trade_from_dict
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal, ann_segment
    annotations = [ann_segment("2024-01-01", "2024-01-10", 10, 12, "#ff9800")]
    signal = TradeSignal(symbol="TEST", action="BUY", pattern="test", timeframe="1d", confidence=1, price=12, qty=1, chart_annotations=annotations)
    candle = OHLCVCandle(open=12, high=13, low=11, close=12, volume=100, timestamp=datetime(2024, 1, 10))
    trade = _open_trade(signal, candle, 10)
    assert _trade_from_dict(_trade_to_dict(trade)).chart_annotations == annotations
    signal.chart_annotations[0]["start_price"] = 99
    assert trade.chart_annotations[0]["start_price"] == 10


def test_legacy_recovery_uses_entry_bar_only(monkeypatch):
    from analysis.chart_renderer import _recover_trade_annotations
    from types import SimpleNamespace
    import core.backtester
    seen = []
    class Detector:
        name = "legacy"
        def analyze(self, snapshot, store):
            seen.append(len(store.get_df("TEST", "1d")))
            return SimpleNamespace(action="BUY", chart_annotations=[{"type": "hline", "price": 10}])
    monkeypatch.setattr(core.backtester, "_iter_pattern_classes", lambda: [("legacy", Detector)])
    idx = pd.bdate_range("2024-01-01", periods=40)
    df = pd.DataFrame({"Open": 10, "High": 11, "Low": 9, "Close": 10, "Volume": 100}, index=idx)
    assert _recover_trade_annotations(df, "TEST", "1d", "legacy", "BUY", idx[20], "America/New_York")
    assert seen == [21]


def test_desktop_pattern_segment_interpolates_when_panned():
    from ui.tv_chart import TradingViewChart
    from unittest.mock import Mock
    chart = object.__new__(TradingViewChart)
    chart._canvas = Mock()
    chart._candles = [{"time": str(i)} for i in range(10)]
    chart._payload = {"segments": [{"data": [{"time": "0", "value": 10}, {"time": "9", "value": 19}], "color": "#ff9800"}]}
    chart._start = 3
    chart._visible = 4
    chart._plot = (0, 0, 400, 100)
    chart._price_lo, chart._price_hi = 10, 20
    chart._draw_pattern_segments(chart._candles[3:7])
    args = chart._canvas.create_line.call_args.args
    assert args == (50, 70, 350, 40)
