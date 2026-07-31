"""Unit tests for analysis.price_volume — RVOL, OBV slope, volume_confirm_gate."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from analysis.indicator_engine import IndicatorEngine
from analysis.price_volume import (
    obv_slope,
    relative_volume,
    volume_confirm_gate,
)
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _make_df(
    n: int = 40,
    *,
    close_path: np.ndarray | None = None,
    volumes: np.ndarray | None = None,
) -> pd.DataFrame:
    if close_path is None:
        close_path = np.linspace(100.0, 110.0, n)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    return pd.DataFrame({
        "open": close_path,
        "high": close_path + 1.0,
        "low": close_path - 1.0,
        "close": close_path,
        "volume": volumes,
    })


def _store_from_df(df: pd.DataFrame, symbol: str = "TEST", timeframe: str = "1d") -> OHLCVStore:
    store = OHLCVStore(window=len(df) + 10)
    base = datetime(2024, 1, 2, tzinfo=timezone.utc)
    candles = []
    for i, row in df.iterrows():
        ts = base + timedelta(days=int(i) if isinstance(i, (int, np.integer)) else i)
        candles.append(OHLCVCandle(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            timestamp=ts,
        ))
    store.replace_all(symbol, timeframe, candles)
    return store


def _signal(action: str = "BUY", symbol: str = "TEST") -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        pattern="test_pattern",
        timeframe="1d",
        confidence=0.8,
        price=110.0,
        qty=10.0,
    )


def test_relative_volume_spike():
    n = 40
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 2_000_000.0  # 2× vs SMA of prior 19 ≈ 1M → rvol ≈ 2.0
    ind = IndicatorEngine(_make_df(n, volumes=vols))
    rvol = relative_volume(ind)
    assert rvol is not None
    assert abs(rvol - 2.0) < 0.01


def test_relative_volume_dry():
    n = 40
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 500_000.0
    ind = IndicatorEngine(_make_df(n, volumes=vols))
    rvol = relative_volume(ind)
    assert rvol is not None
    assert abs(rvol - 0.5) < 0.01


def test_relative_volume_insufficient():
    ind = IndicatorEngine(_make_df(10))
    assert relative_volume(ind) is None


def test_obv_slope_rising_on_up_volume():
    # Rising closes + volume → positive OBV slope
    n = 40
    closes = np.linspace(100.0, 120.0, n)
    ind = IndicatorEngine(_make_df(n, close_path=closes))
    slope = obv_slope(ind, bars=5)
    assert slope is not None
    assert slope > 0


def test_obv_slope_falling_on_down_volume():
    n = 40
    closes = np.linspace(120.0, 100.0, n)
    ind = IndicatorEngine(_make_df(n, close_path=closes))
    slope = obv_slope(ind, bars=5)
    assert slope is not None
    assert slope < 0


def test_gate_pass_buy_high_rvol_rising_obv():
    n = 40
    closes = np.linspace(100.0, 120.0, n)
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 2_000_000.0
    store = _store_from_df(_make_df(n, close_path=closes, volumes=vols))
    verdict = volume_confirm_gate(_signal("BUY"), store, rvol_min=1.5, obv_bars=5)
    assert verdict.passed
    assert verdict.rvol is not None and verdict.rvol >= 1.5
    assert verdict.obv_slope is not None and verdict.obv_slope >= 0


def test_gate_reject_low_rvol():
    n = 40
    closes = np.linspace(100.0, 120.0, n)
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 800_000.0
    store = _store_from_df(_make_df(n, close_path=closes, volumes=vols))
    verdict = volume_confirm_gate(_signal("BUY"), store, rvol_min=1.5, obv_bars=5)
    assert not verdict.passed
    assert "rvol=" in verdict.reason


def test_gate_reject_buy_falling_obv():
    n = 40
    # Falling price → negative OBV; high volume on last bar still fails OBV check
    closes = np.linspace(120.0, 100.0, n)
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 2_500_000.0
    store = _store_from_df(_make_df(n, close_path=closes, volumes=vols))
    verdict = volume_confirm_gate(_signal("BUY"), store, rvol_min=1.5, obv_bars=5)
    assert not verdict.passed
    assert "OBV" in verdict.reason


def test_gate_reject_sell_rising_obv():
    n = 40
    closes = np.linspace(100.0, 120.0, n)
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 2_500_000.0
    store = _store_from_df(_make_df(n, close_path=closes, volumes=vols))
    verdict = volume_confirm_gate(_signal("SELL"), store, rvol_min=1.5, obv_bars=5)
    assert not verdict.passed
    assert "OBV" in verdict.reason


def test_gate_fail_open_short_history():
    store = _store_from_df(_make_df(10))
    verdict = volume_confirm_gate(_signal("BUY"), store, rvol_min=1.5)
    assert verdict.passed
    assert "fail-open" in verdict.reason


def test_gate_tags_signal_metrics():
    n = 40
    closes = np.linspace(100.0, 120.0, n)
    vols = np.full(n, 1_000_000.0)
    vols[-1] = 2_000_000.0
    store = _store_from_df(_make_df(n, close_path=closes, volumes=vols))
    sig = _signal("BUY")
    volume_confirm_gate(sig, store, rvol_min=1.5)
    assert sig.rvol is not None
    assert sig.obv_slope is not None


if __name__ == "__main__":
    test_relative_volume_spike()
    test_relative_volume_dry()
    test_relative_volume_insufficient()
    test_obv_slope_rising_on_up_volume()
    test_obv_slope_falling_on_down_volume()
    test_gate_pass_buy_high_rvol_rising_obv()
    test_gate_reject_low_rvol()
    test_gate_reject_buy_falling_obv()
    test_gate_reject_sell_rising_obv()
    test_gate_fail_open_short_history()
    test_gate_tags_signal_metrics()
    print("volume_gate: all checks passed")
