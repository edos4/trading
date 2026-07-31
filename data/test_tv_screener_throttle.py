"""Unit tests for TradingView screener throttle + 429 retry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from config import settings
from data import tv_client


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    monkeypatch.setattr(settings, "tv_screener_min_interval_seconds", 0.0)
    monkeypatch.setattr(settings, "tv_screener_max_retries", 3)
    monkeypatch.setattr(settings, "tv_screener_retry_backoff_seconds", 0.01)
    tv_client.reset_screener_throttle_for_tests()
    yield
    tv_client.reset_screener_throttle_for_tests()


def _http_429() -> requests.HTTPError:
    resp = SimpleNamespace(status_code=429, text="")
    err = requests.HTTPError(
        "429 Client Error: Too Many Requests\n Body: \n for url: "
        "https://scanner.tradingview.com/america/scan"
    )
    err.response = resp  # type: ignore[attr-defined]
    return err


def test_is_screener_429_detects_status_and_message():
    assert tv_client._is_screener_429(_http_429())
    assert tv_client._is_screener_429(
        RuntimeError("429 Client Error: Too Many Requests")
    )
    assert not tv_client._is_screener_429(RuntimeError("500 Server Error"))


def test_get_scanner_data_retries_on_429_then_succeeds():
    query = MagicMock()
    ok = (1, pd.DataFrame({"name": ["AAPL"]}))
    query.get_scanner_data.side_effect = [_http_429(), _http_429(), ok]

    with patch.object(tv_client.time, "sleep") as sleep:
        count, df = tv_client._get_scanner_data(query)

    assert count == 1
    assert list(df["name"]) == ["AAPL"]
    assert query.get_scanner_data.call_count == 3
    assert sleep.call_count == 2  # two 429s before success


def test_get_scanner_data_exhausted_retries_raises():
    query = MagicMock()
    query.get_scanner_data.side_effect = _http_429()

    with patch.object(tv_client.time, "sleep"):
        with pytest.raises(requests.HTTPError):
            tv_client._get_scanner_data(query)

    # initial + 3 retries = 4 attempts
    assert query.get_scanner_data.call_count == 4


def test_throttle_enforces_min_interval(monkeypatch):
    monkeypatch.setattr(settings, "tv_screener_min_interval_seconds", 0.25)
    tv_client.reset_screener_throttle_for_tests()

    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch.object(tv_client.time, "sleep", side_effect=_record_sleep):
        # First call reserves the next slot; second should wait.
        tv_client._throttle_screener()
        tv_client._throttle_screener()

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.25, abs=0.05)


def test_history_reuses_latest_candle_without_second_screener():
    client = tv_client.TVClient(screener="america", exchange="NASDAQ")
    latest = tv_client.OHLCVCandle(1.0, 2.0, 0.5, 1.5, 100.0)
    chart = [
        tv_client.OHLCVCandle(
            1.0, 1.0, 1.0, 1.0, 10.0, timestamp=None
        ),
        tv_client.OHLCVCandle(
            1.1, 1.2, 1.0, 1.15, 20.0, timestamp=None
        ),
    ]

    with (
        patch.object(client, "_fetch_history_chart", return_value=chart),
        patch.object(client, "_fetch_candle_screener") as fetch_candle,
    ):
        out = client._fetch_history_screener(
            "AAPL", "NASDAQ", "1d", latest=latest
        )

    fetch_candle.assert_not_called()
    assert out[-1].close == 1.5
    assert out[-1].volume == 100.0
