"""Live scans must confirm patterns using only the history they have received."""

import importlib
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from analysis.indicator_engine import IndicatorEngine
from patterns import _dedup
from patterns._channels import find_channel


@pytest.fixture(autouse=True)
def clear_walk():
    _dedup.reset()
    yield
    _dedup.reset()


def _frame(close):
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                         "close": close, "volume": 1000.0},
                        index=pd.bdate_range("2024-01-02", periods=len(close)))


def _channel(n=51, *, mirrored=False):
    close = np.interp(np.arange(n), [0, 20, 30, 45, 46, 47, 48, 49, 50],
                      [80, 120, 110, 130, 127, 122, 119, 116, 114])
    rsi = pd.Series(50.0, index=range(n))
    rsi.iloc[20], rsi.iloc[45] = 70.0, 60.0
    if mirrored:
        close, rsi = 300 - close, 100 - rsi
    return _frame(close), rsi


def test_channel_fires_on_latest_confirmed_break_and_does_not_cache_missing_break():
    df, rsi = _channel()
    assert find_channel(IndicatorEngine(df.iloc[:-1]), rsi.iloc[:-1], 49, "up") is None
    setup = find_channel(IndicatorEngine(df), rsi, 50, "up")
    assert setup is not None
    assert (setup.first, setup.second, setup.entry) == (20, 45, 50)
    # Another symbol can reuse these indices without reusing the first symbol's prices.
    other = find_channel(IndicatorEngine(df * 2), rsi, 50, "up")
    assert other is not None
    assert other.first_price == 2 * setup.first_price
    assert other.entry_line == 2 * setup.entry_line


def test_historical_channel_cache_distinguishes_mirrored_direction():
    _dedup.set_current(50)
    for direction, mirrored in (("up", False), ("down", True)):
        df, rsi = _channel(100, mirrored=mirrored)
        setup = find_channel(IndicatorEngine(df), rsi, 50, direction)
        assert setup is not None
        assert setup.entry == 50
        assert np.sign(setup.slope) == (-1 if mirrored else 1)


def test_live_channel_waits_for_second_peak_confirmation():
    df, rsi = _channel(49)
    for bar, close in ((46, 114.0), (47, 113.0), (48, 112.0)):
        df.iloc[bar] = [close, close + 1, close - 1, close, 1000.0]
    assert find_channel(IndicatorEngine(df.iloc[:-1]), rsi.iloc[:-1], 47, "up") is None
    setup = find_channel(IndicatorEngine(df), rsi, 48, "up")
    assert setup is not None
    assert setup.entry == 48


def _double_top(monkeypatch, n=48, break_bar=None):
    close = np.interp(np.arange(n), [0, 20, 30, 40, 47, 90], [100, 120, 103, 115, 107, 106])
    if break_bar is not None:
        close[break_bar:] = np.linspace(100, 99, n - break_bar)
    df = _frame(close)

    def rsi(engine, period):
        values = pd.Series(50.0, index=range(len(engine.close)))
        values.iloc[20], values.iloc[40] = 75.0, 55.0
        return values

    monkeypatch.setattr(IndicatorEngine, "rsi_wilder", rsi)
    pattern = importlib.import_module("patterns.002_double_top").DoubleTopPattern()
    store = SimpleNamespace(get_df=lambda *args, **kwargs: df)
    snapshot = SimpleNamespace(symbol="TEST", timeframe="1d")
    return pattern, snapshot, store


def test_live_double_top_day_seven_does_not_require_future_break(monkeypatch):
    pattern, snapshot, store = _double_top(monkeypatch)
    signal = pattern.analyze(snapshot, store)
    assert signal is not None
    assert signal.action == "SELL"
    assert signal.price == 107.0
    assert signal.setup_key == (20, 40)
    assert signal.neckline == 102.0
    # Historical parity intentionally still requires a later confirmed break.
    _dedup.set_current(47)
    assert pattern.analyze(snapshot, store) is None


def test_live_double_top_waits_for_peak_confirmation(monkeypatch):
    pattern, snapshot, store = _double_top(monkeypatch, n=44, break_bar=41)
    signal = pattern.analyze(snapshot, store)
    assert signal is not None
    assert signal.price == 99.0
    assert "break@41" in signal.notes
    assert signal.neckline == 102.0


def test_pending_double_top_does_not_consume_other_symbols_pivots(monkeypatch):
    pattern, snapshot, store = _double_top(monkeypatch, n=46)
    assert pattern.analyze(snapshot, store) is None
    assert not _dedup.used(20)
    pattern, snapshot, store = _double_top(monkeypatch)
    assert pattern.analyze(snapshot, store) is not None
