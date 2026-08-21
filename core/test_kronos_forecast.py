from __future__ import annotations

import pandas as pd
import pytest

from core.kronos_forecast import (
    build_kronos_viewer_payload,
    clamp_pred_days,
    normalize_symbol,
    predict_ohlc,
)


class _FakePredictor:
    def predict(self, *, pred_len, **_kwargs):
        idx = range(pred_len)
        close = pd.Series([101.0 + i for i in idx], dtype=float)
        return pd.DataFrame({
            "open": close - 0.3,
            "high": close + 0.5,
            "low": close - 0.6,
            "close": close,
            "volume": 1_000.0,
        })


def _hist(n: int = 80) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = pd.Series(range(20, 20 + n), index=idx, dtype=float)
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.4,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)


def test_normalize_symbol() -> None:
    assert normalize_symbol(" aapl ") == "AAPL"
    with pytest.raises(ValueError):
        normalize_symbol("")
    with pytest.raises(ValueError):
        normalize_symbol("AAPL;DROP")


def test_clamp_pred_days() -> None:
    assert clamp_pred_days(5) == 5
    with pytest.raises(ValueError):
        clamp_pred_days(0)
    with pytest.raises(ValueError):
        clamp_pred_days(500)


def test_predict_ohlc_sets_future_bdates() -> None:
    df = _hist()
    pred = predict_ohlc(_FakePredictor(), df, 5, sample_count=1, lookback=60)
    assert len(pred) == 5
    assert pred.index[0] > df.index[-1]
    assert pred["close"].iloc[0] == 101.0


def test_viewer_payload_shows_kronos_path() -> None:
    actual = _hist(40)
    pred_idx = pd.bdate_range(start=actual.index[-1], periods=4, freq="B")[1:]
    pred = pd.DataFrame({
        "open": [140.0, 141.0, 142.0],
        "high": [141.0, 142.0, 143.0],
        "low": [139.0, 140.0, 141.0],
        "close": [140.5, 141.5, 142.5],
        "volume": [0.0, 0.0, 0.0],
    }, index=pred_idx)
    payload = build_kronos_viewer_payload(actual, pred, symbol="AAPL")
    assert payload["pred_candles"][-1]["close"] == 142.5
    assert payload["pred_candles"][0]["predicted"] is True
    assert payload["forecast"][0]["time"] == payload["candles"][-1]["time"]
    assert payload["forecast"][-1]["value"] == 142.5
    assert payload["pred"]["days"] == 3
    assert any(m.get("text") == "Kronos" for m in payload["markers"])
    assert "Kronos" in payload["title"]
