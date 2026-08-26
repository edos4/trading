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


def test_local_backfill_only_on_history_owner() -> None:
    with patch("config.settings") as s:
        s.stocks_history_url = "https://33ai.edos.uk"
        assert local_history_backfill_enabled() is False
    with patch("data.history.owns_local_stocks_history", return_value=False):
        with patch("config.settings") as s:
            s.stocks_history_url = ""
            assert local_history_backfill_enabled() is False
    with patch("data.history.owns_local_stocks_history", return_value=True):
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


def test_history_path_encodes_slash_tickers() -> None:
    from data.history_client import _history_path

    assert _history_path("AAPL") == "/api/history/AAPL"
    assert _history_path("FLG/PU") == "/api/history/FLG%2FPU"
    assert _history_path("AAPL", "/meta") == "/api/history/AAPL/meta"


def test_load_daily_bars_forwards_limit() -> None:
    from data.history import load_daily_bars

    with patch("data.history_client.history_api_configured", return_value=False), \
         patch("data.db.load_daily_ohlcv_rows", return_value=[]) as load:
        load_daily_bars("AAPL", limit=512)
    load.assert_called_once_with("AAPL", after_ts=None, limit=512)


def test_enable_ui_web_sets_33ai_on_laptop() -> None:
    from config import settings
    from data.history import (
        DEFAULT_STOCKS_HISTORY_URL,
        disable_ui_web_history,
        enable_ui_web_history,
        ui_web_history_enabled,
    )

    prev_url = settings.stocks_history_url
    prev_owner = settings.stocks_history_owner
    try:
        settings.stocks_history_url = ""
        settings.stocks_history_owner = False
        with patch("data.history.owns_local_stocks_history", return_value=False):
            enable_ui_web_history()
        assert ui_web_history_enabled() is True
        assert settings.stocks_history_url == DEFAULT_STOCKS_HISTORY_URL
    finally:
        disable_ui_web_history()
        settings.stocks_history_url = prev_url
        settings.stocks_history_owner = prev_owner


def test_enable_ui_web_keeps_empty_url_on_owner() -> None:
    from config import settings
    from data.history import disable_ui_web_history, enable_ui_web_history

    prev_url = settings.stocks_history_url
    prev_owner = settings.stocks_history_owner
    try:
        settings.stocks_history_url = ""
        settings.stocks_history_owner = True
        enable_ui_web_history()
        assert settings.stocks_history_url == ""
    finally:
        disable_ui_web_history()
        settings.stocks_history_url = prev_url
        settings.stocks_history_owner = prev_owner


def test_enable_ui_web_respects_explicit_url() -> None:
    from config import settings
    from data.history import disable_ui_web_history, enable_ui_web_history

    prev_url = settings.stocks_history_url
    try:
        settings.stocks_history_url = "https://other.example"
        with patch("data.history.owns_local_stocks_history", return_value=False):
            enable_ui_web_history()
        assert settings.stocks_history_url == "https://other.example"
    finally:
        disable_ui_web_history()
        settings.stocks_history_url = prev_url


def test_fetch_no_tv_in_ui_web_mode() -> None:
    from data.history import disable_ui_web_history, enable_ui_web_history

    class _TV:
        def _fetch_history_chart(self, symbol, timeframe):
            raise AssertionError("Yahoo must not run in --ui/--web")

        def _fetch_history_screener(self, symbol, exchange, timeframe):
            raise AssertionError("Yahoo must not run in --ui/--web")

    try:
        with patch("data.history.owns_local_stocks_history", return_value=True):
            enable_ui_web_history()
        with patch("data.history.load_daily_candles", return_value=None):
            out = fetch_ohlcv_candles(
                "ZZZZ", "1d", tv_client=_TV(), tv_fallback=True,
            )
        assert out == []
    finally:
        disable_ui_web_history()


def test_tv_client_chart_uses_facade_in_ui_web() -> None:
    from data.history import disable_ui_web_history, enable_ui_web_history
    from data.tv_client import TVClient

    bars = bars_to_candles([_bar(1704456000, 10.0)])
    try:
        with patch("data.history.owns_local_stocks_history", return_value=True):
            enable_ui_web_history()
        client = TVClient("america", "NASDAQ")
        with patch("data.history.load_daily_candles", return_value=bars), \
             patch("data.tv_client._yahoo_chart_payload") as yahoo:
            out = client._fetch_history_chart("AAPL", "1d")
        yahoo.assert_not_called()
        assert len(out) == 1
        assert out[0].close == 10.0
    finally:
        disable_ui_web_history()
