"""Unit tests for the stocks_history facade (no live HTTP / Postgres)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from data.history import (
    bars_to_candles,
    bars_to_df,
    fetch_ohlcv_candles,
    local_history_backfill_enabled,
    resample_weekly,
    resample_chart_frame,
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


def test_weekly_chart_aggregation_uses_market_dates_across_us_dst() -> None:
    idx = (pd.bdate_range("2024-03-04", periods=10, tz="America/New_York")
           + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC")
    daily = pd.DataFrame({
        "open": range(10, 20), "high": range(12, 22), "low": range(9, 19),
        "close": range(11, 21), "volume": 100,
    }, index=idx)

    weekly = resample_chart_frame(daily, "1W", session_tz="America/New_York")

    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2024-03-08", "2024-03-15"]
    assert weekly.iloc[0].to_dict() == {
        "open": 10, "high": 16, "low": 9, "close": 15, "volume": 500,
    }
    assert weekly.iloc[1].to_dict() == {
        "open": 15, "high": 21, "low": 14, "close": 20, "volume": 500,
    }
    assert weekly.index.tz is None  # chart labels are already market session dates
    assert resample_chart_frame(daily, "1d", session_tz="America/New_York") is daily


@pytest.mark.parametrize("mixed_prints", [False, True])
def test_weekly_chart_aggregates_date_only_history_and_actual_prints(mixed_prints) -> None:
    dates = pd.bdate_range("2024-03-04", periods=10)
    rows = [{"date": day.date().isoformat(), "open": 10 + i, "high": 11 + i,
             "low": 9 + i, "close": 10 + i, "volume": 100}
            for i, day in enumerate(dates)]
    if mixed_prints:
        # Exchange opens span DST, and Saturday's early UTC print is still
        # Friday in New York. Keep the latest valid print in that session.
        rows.extend({"date": stamp, "open": close, "high": close + 1,
                     "low": close - 1, "close": close, "volume": 100}
                    for stamp, close in [
                        ("2024-03-08T14:30:00+00:00", 40),
                        ("2024-03-09T01:00:00+00:00", 50),
                        ("2024-03-11T13:30:00+00:00", 60),
                    ])
    daily = bars_to_df(list(reversed(rows)))
    original = daily.copy()

    weekly = resample_chart_frame(daily, "1W", session_tz="America/New_York")

    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2024-03-08", "2024-03-15"]
    assert weekly.iloc[0].to_dict() == {
        "open": 10, "high": 51 if mixed_prints else 15, "low": 9,
        "close": 50 if mixed_prints else 14, "volume": 500,
    }
    assert weekly.iloc[1].to_dict() == {
        "open": 60 if mixed_prints else 15, "high": 61 if mixed_prints else 20,
        "low": 15 if mixed_prints else 14, "close": 19, "volume": 500,
    }
    pd.testing.assert_frame_equal(daily, original)


def test_weekly_chart_handles_ph_midnight_dates_duplicates_and_partial_source_week() -> None:
    # Local midnight is the preceding UTC day in Manila. A chart must group by
    # the local session, retain the latest reprint, and stop at its source tape.
    idx = pd.bdate_range("2024-05-06", periods=8, tz="Asia/Manila").tz_convert("UTC")
    daily = pd.DataFrame({
        "Open": range(10, 18), "High": range(12, 20), "Low": range(9, 17),
        "Close": range(11, 19), "Volume": 100,
    }, index=idx)
    reprint = daily.iloc[[4]].copy()
    reprint.index += pd.Timedelta(hours=1)
    reprint.loc[:, ["High", "Close"]] = [99, 98]
    source = pd.concat([reprint, daily.iloc[::-1]])

    weekly = resample_chart_frame(source, "1W", session_tz="Asia/Manila")

    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2024-05-10", "2024-05-17"]
    assert weekly.iloc[0].to_dict() == {
        "open": 10, "high": 99, "low": 9, "close": 98, "volume": 500,
    }
    # Only Monday through Wednesday were supplied for the final week.
    assert weekly.iloc[1].to_dict() == {
        "open": 15, "high": 19, "low": 14, "close": 18, "volume": 300,
    }


@pytest.mark.parametrize("invalid_close", [float("nan"), float("inf"), "invalid"])
def test_weekly_chart_keeps_valid_session_when_latest_reprint_is_invalid(invalid_close) -> None:
    idx = pd.DatetimeIndex(["2024-01-01T14:30Z", "2024-01-02T14:30Z", "2024-01-05T14:30Z"])
    daily = pd.DataFrame({
        "open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11],
        "close": [10, 11, 12], "volume": 100,
    }, index=idx)
    invalid = daily.iloc[[-1]].copy().astype({"close": object})
    invalid.index += pd.Timedelta(hours=1)
    invalid.loc[:, "close"] = invalid_close
    invalid.loc[:, "volume"] = 999
    source = pd.concat([invalid, daily])

    weekly = resample_chart_frame(source, "1W", session_tz="America/New_York")

    assert weekly.index[0] == pd.Timestamp("2024-01-05")
    assert weekly.iloc[0].to_dict() == {
        "open": 10, "high": 13, "low": 9, "close": 12, "volume": 300,
    }
    assert len(source) == 4


def test_bulk_tape_rows_uses_one_http_call_when_remote() -> None:
    bars = [{"ts": 10, "open": 1, "high": 1, "low": 1, "close": 2, "volume": 3}]
    with patch("data.history.local_history_backfill_enabled", return_value=False), \
         patch(
             "data.history_bulk.fetch_history_bars_bulk",
             return_value={"AAPL": bars, "MSFT": []},
         ) as fetch:
        from data.history_bulk import load_daily_tape_rows_bulk

        out = load_daily_tape_rows_bulk(
            ["AAPL", "MSFT"], after_ts=1, limit=420, market="us",
        )
    assert out is not None
    assert out["AAPL"][0]["timestamp"] == 10
    assert out["AAPL"][0]["close"] == 2
    assert out["MSFT"] == []
    fetch.assert_called_once()


def test_bulk_http_missing_route_returns_none() -> None:
    class _Resp:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 must not raise into per-symbol fallback")

        def json(self):
            return {}

    with patch("data.history_bulk._post", return_value=_Resp()):
        from data.history_bulk import fetch_history_bars_bulk

        assert fetch_history_bars_bulk(["AAPL"], after_ts=1, limit=10) is None


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
        s.stocks_history_url = "https://33ai.rootlabs.dev"
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


def test_fetch_history_bars_retries_connect_timeout() -> None:
    import httpx
    from data.history_client import fetch_history_bars

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"bars": [_bar(1, 1.0)]}

    calls = {"n": 0}

    class _Client:
        def get(self, *_a, **_k):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectTimeout("timed out")
            return _Resp()

    with patch("data.history_client.history_api_configured", return_value=True), \
         patch("data.history_client._client", return_value=_Client()), \
         patch("data.history_client.time.sleep"):
        bars = fetch_history_bars("FEDU", limit=10)
    assert bars is not None and len(bars) == 1
    assert calls["n"] == 2


def test_fetch_history_bars_timeout_does_not_reset_shared_client() -> None:
    """Closing the shared httpx.Client on a timeout kills in-flight TLS
    handshakes on the other 3 inflight slots and stamps out more timeouts."""
    import httpx
    from data.history_client import fetch_history_bars

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"bars": [_bar(1, 1.0)]}

    class _Client:
        def get(self, *_a, **_k):
            raise httpx.ReadTimeout("The read operation timed out")

    with patch("data.history_client._client", return_value=_Client()), \
         patch("data.history_client._reset_client") as reset, \
         patch("data.history_client.time.sleep"):
        assert fetch_history_bars("KNSL") is None
    reset.assert_not_called()


def test_load_daily_tape_rows_empty_vs_unavailable() -> None:
    from data.history import load_daily_tape_rows

    with patch("data.history.load_daily_bars", return_value=None):
        assert load_daily_tape_rows("TACT") is None
    with patch("data.history.load_daily_bars", return_value=[]):
        assert load_daily_tape_rows("MAGE") == []



def test_history_path_encodes_slash_tickers() -> None:
    from data.history_client import _history_path

    assert _history_path("AAPL") == "/api/history/AAPL"
    assert _history_path("FLG/PU") == "/api/history/FLG%2FPU"
    assert _history_path("AAPL", "/meta") == "/api/history/AAPL/meta"


def test_load_daily_bars_forwards_limit() -> None:
    from data.history import load_daily_bars

    with patch("data.history.local_history_backfill_enabled", return_value=False), \
         patch("data.history_client.fetch_history_bars", return_value=[]) as load, \
         patch("data.db.get_conn") as get_conn:
        load_daily_bars("AAPL", limit=512)
    load.assert_called_once_with("AAPL", after_ts=None, limit=512, market=None)
    get_conn.assert_not_called()


def test_load_daily_bars_never_uses_postgres() -> None:
    from data.history import load_daily_bars

    with patch("data.history.local_history_backfill_enabled", return_value=False), \
         patch("data.history_client.fetch_history_bars", return_value=None) as load, \
         patch("data.db.load_daily_ohlcv_rows") as db_load, \
         patch("data.db.get_conn") as get_conn:
        assert load_daily_bars("AAPL") is None
    load.assert_called_once()
    db_load.assert_not_called()
    get_conn.assert_not_called()


def test_load_daily_bars_uses_postgres_on_history_owner() -> None:
    from data.history import load_daily_bars

    rows = [_bar(1, 10.0)]
    with patch("data.history.local_history_backfill_enabled", return_value=True), \
         patch("data.db.load_daily_ohlcv_rows", return_value=rows) as db_load, \
         patch("data.history_client.fetch_history_bars") as api:
        assert load_daily_bars("AAPL", after_ts=9, limit=10) == rows
    db_load.assert_called_once_with("AAPL", after_ts=9, limit=10, market=None)
    api.assert_not_called()


def test_history_client_defaults_to_33ai() -> None:
    from config import settings
    from data.history_client import DEFAULT_STOCKS_HISTORY_URL, _base_url

    prev = settings.stocks_history_url
    try:
        settings.stocks_history_url = ""
        assert _base_url() == DEFAULT_STOCKS_HISTORY_URL
        settings.stocks_history_url = "https://other.example/"
        assert _base_url() == "https://other.example"
    finally:
        settings.stocks_history_url = prev


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


def test_list_history_symbols_uses_api_when_configured() -> None:
    from data.history import list_history_symbols

    rows = [{"symbol": "AAPL", "row_count": 10}]
    with patch("data.history.local_history_backfill_enabled", return_value=False), \
         patch("data.history_client.fetch_history_symbols", return_value=rows) as fetch, \
         patch("data.db.get_conn") as get_conn, \
         patch("data.db.all_symbols") as all_sym:
        assert list_history_symbols() == rows
    fetch.assert_called_once_with(market=None)
    get_conn.assert_not_called()
    all_sym.assert_not_called()


def test_iter_ticker_frames_skips_thin_and_empty(monkeypatch) -> None:
    import pandas as pd
    from learn.dataset import iter_ticker_frames

    fat = pd.DataFrame(
        {"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 1]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

    def fake_list():
        return [
            {"symbol": "THIN", "row_count": 1},
            {"symbol": "FAT", "row_count": 400},
            {"symbol": "EMPTY", "row_count": 400},
        ]

    def fake_df(symbol, **_k):
        if symbol == "FAT":
            return fat
        if symbol == "EMPTY":
            return None
        raise AssertionError(f"should not load {symbol}")

    monkeypatch.setattr("data.history.list_history_symbols", fake_list)
    monkeypatch.setattr("data.history.load_daily_ohlcv_df", fake_df)
    out = list(iter_ticker_frames(min_bars=2))
    assert [(s, len(df)) for s, df in out] == [("FAT", 2)]
