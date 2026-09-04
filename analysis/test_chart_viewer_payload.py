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
