"""Unit tests for the stocks_history facade (no live HTTP / Postgres)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from data.history import (
    bars_to_candles,
    fetch_ohlcv_candles,
    local_history_backfill_enabled,
    resample_weekly,
)
from data.tv_client import OHLCVCandle


def _bar(ts: int, close: float) -> dict:
    return {
        "ts": ts,
        "date": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
    }


def test_bars_to_candles_and_weekly() -> None:
    # ~10 weekdays
    bars = [_bar(1704456000 + i * 86400, 10.0 + i) for i in range(12)]
    candles = bars_to_candles(bars)
    assert len(candles) == 12
    assert candles[0].close == 10.0
    weekly = resample_weekly(candles)
    assert weekly
    assert weekly[-1].high >= weekly[-1].low


def test_fetch_uses_api_bars_without_tv() -> None:
    bars = [_bar(1704456000 + i * 86400, 50.0) for i in range(5)]

    with patch("data.history.load_daily_candles", return_value=bars_to_candles(bars)):
        out = fetch_ohlcv_candles("AAPL", "1d", tv_fallback=True)
    assert len(out) == 5
    assert out[-1].close == 50.0


def test_fetch_tv_fallback_when_empty() -> None:
    tv_bar = OHLCVCandle(
        open=1, high=2, low=0.5, close=1.5, volume=10,
        timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )

    class _TV:
        def _fetch_history_chart(self, symbol, timeframe):
            return [tv_bar]

        def _fetch_history_screener(self, symbol, exchange, timeframe):
            return [tv_bar]

    with patch("data.history.load_daily_candles", return_value=None):
        out = fetch_ohlcv_candles("ZZZZ", "1d", tv_client=_TV(), tv_fallback=True)
    assert out == [tv_bar]


def test_local_backfill_off_when_url_set() -> None:
    with patch("config.settings") as s:
        s.stocks_history_url = "https://33ai.edos.uk"
        assert local_history_backfill_enabled() is False
    with patch("config.settings") as s:
        s.stocks_history_url = ""
        assert local_history_backfill_enabled() is True


def test_stocks_history_auth_defaults_to_username() -> None:
    from config import Settings

    s = Settings.model_construct(
        web_ui_username="admin",
        web_ui_password="dashboard-secret",
        stocks_history_username="",
        stocks_history_password="",
    )
    assert s.stocks_history_auth == ("admin", "admin")


def test_load_daily_bars_forwards_limit() -> None:
    from data.history import load_daily_bars

    with patch("data.history_client.history_api_configured", return_value=False), \
         patch("data.db.load_daily_ohlcv_rows", return_value=[]) as load:
        load_daily_bars("AAPL", limit=512)
    load.assert_called_once_with("AAPL", after_ts=None, limit=512)
