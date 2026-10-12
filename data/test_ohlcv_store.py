"""Cached candle frames preserve values, timezone conversion, and invalidation."""

from datetime import datetime, timezone

import pandas as pd
import pytest

from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle


@pytest.mark.parametrize("session_tz, expected_days", [
    ("America/New_York", ["2026-01-04", "2026-01-05"]),
    ("Asia/Manila", ["2026-01-05", "2026-01-06"]),
])
def test_frame_values_session_dates_and_cache_invalidation(session_tz, expected_days):
    candles = [OHLCVCandle(1, 3, 0.5, 2, 100, datetime(2026, 1, day, tzinfo=timezone.utc))
               for day in (5, 6)]
    store = OHLCVStore(session_tz=session_tz)
    store.replace_all("AAA", "1d", candles)
    frame = store.get_df("AAA", "1d")
    expected = pd.DataFrame([[1, 3, 0.5, 2, 100]] * 2,
                            index=pd.DatetimeIndex(expected_days, name="timestamp"),
                            columns=["open", "high", "low", "close", "volume"])
    pd.testing.assert_frame_equal(frame, expected)
    assert store.get_df("AAA", "1d") is frame
    assert store.get_df("AAA", "1d", min_bars=3) is None
    store.apply_candle("AAA", "1d", OHLCVCandle(1, 4, 0.5, 3, 200, candles[-1].timestamp))
    updated = store.get_df("AAA", "1d")
    assert updated.iloc[-1]["close"] == 3
    assert updated is not frame
    assert frame.iloc[-1]["close"] == 2
