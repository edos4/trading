"""Patternsv2 fast-scan paths keep the detector's answers unchanged."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns_v2.patterns import detect
from patterns_v2.patterns.common import atr, bar_sane, sane_mask, zigzag
from patterns_v2.signals import (
    detect_raw,
    detect_trade_signals,
    signals_from_raw,
)
from core.pattern_jobs import detect_v2_batch


def _series(seed: int, n: int = 200):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(rng.normal(0, 0.01, n).cumsum())
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    return high, low, close


def _poisoned(seed: int):
    """Series with invalid prints, so the mask has something to reject."""
    high, low, close = _series(seed)
    rng = np.random.default_rng(seed + 1)
    for i in rng.integers(0, len(close), 6):
        kind = int(rng.integers(0, 4))
        if kind == 0:
            high[i] = low[i] = close[i]
        elif kind == 1:
            high[i] = np.nan
        elif kind == 2:
            close[i] = high[i] * 1.5
        else:
            high[i], low[i] = low[i], high[i]
    return high, low, close


@pytest.mark.parametrize("seed", range(8))
def test_vectorized_sanity_mask_matches_scalar(seed):
    high, low, close = _poisoned(seed)
    mask = sane_mask(high, low, close)
    assert [bool(mask[i]) for i in range(len(close))] == [
        bar_sane(high, low, close, i) for i in range(len(close))
    ]


@pytest.mark.parametrize("seed", range(12))
def test_latest_bar_hint_keeps_the_current_bar_identical(seed):
    high, low, close = _poisoned(seed)
    latest = len(close) - 1
    full = [s for s in detect(high, low, close) if s["signal"] == latest]
    live = [s for s in detect(high, low, close, latest=latest) if s["signal"] == latest]
    key = lambda s: (s["pattern"], s["signal"], s["direction"])
    assert sorted(full, key=key) == sorted(live, key=key)


@pytest.mark.parametrize("seed", range(6))
def test_array_detection_matches_frame_detection(seed):
    high, low, close = _series(seed)
    index = pd.date_range("2024-01-01", periods=len(close), freq="B")
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close,
         "volume": np.full(len(close), 1000.0)},
        index=index,
    )
    expected = detect_trade_signals(
        df, symbol="TEST", timeframe="1d", latest_bar_only=True,
    )
    found = detect_raw(high, low, close, latest_bar_only=True)
    got = signals_from_raw(
        df, found, symbol="TEST", timeframe="1d", latest_bar_only=True,
    )
    assert [(s.pattern, s.signal_bar_idx) for s in got] == [
        (s.pattern, s.signal_bar_idx) for s in expected
    ]


def test_zigzag_uses_the_mask_without_changing_pivots():
    high, low, close = _poisoned(3)
    piv = zigzag(high, low, close, atr(high, low, close))
    assert len(piv[0]) == len(piv[1]) == len(piv[2]) == len(piv[3])


def test_detect_arrays_cache_follows_store_version():
    store = OHLCVStore(window=64)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    candles = [
        OHLCVCandle(
            open=100 + i, high=101 + i, low=99 + i, close=100.5 + i,
            volume=1000, timestamp=start + timedelta(days=i),
        )
        for i in range(20)
    ]
    store.replace_all("TEST", "1d", candles)
    arrays = store.detect_arrays("TEST", "1d", min_bars=2)
    assert arrays is not None
    assert np.allclose(arrays[0], [c.high for c in candles])
    assert store.detect_arrays("TEST", "1d", min_bars=2) is arrays

    store.append_candle("TEST", "1d", OHLCVCandle(
        open=120, high=121, low=119, close=120.5, volume=1000,
        timestamp=start + timedelta(days=20),
    ))
    refreshed = store.detect_arrays("TEST", "1d", min_bars=2)
    assert refreshed is not arrays
    assert len(refreshed[0]) == 21


def test_process_batch_entry_matches_inline_detection():
    jobs, expected = [], []
    for seed in range(4):
        high, low, close = _series(seed)
        jobs.append((f"T{seed}", "1d", high, low, close))
        expected.append(detect_raw(high, low, close, latest_bar_only=True))
    assert detect_v2_batch(jobs) == expected
