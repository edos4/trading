from __future__ import annotations

from datetime import datetime

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
