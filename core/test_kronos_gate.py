"""Unit checks for core/kronos_gate without loading real Kronos weights."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from core.kronos_eval import LOOKBACK
from core.kronos_gate import KronosGate, _context_lookback
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _fill_store(n: int | None = None, last_close: float = 100.0) -> OHLCVStore:
    n = max(LOOKBACK, _context_lookback()) if n is None else n
    store = OHLCVStore(window=DEFAULT_WINDOW)
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    for i in range(n):
        # Flat path ending at last_close — gate only cares about forecast vs close.
        c = last_close
        store.append_candle(
            "TEST",
            "1d",
            OHLCVCandle(
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1_000_000.0,
                timestamp=start + timedelta(days=i),
            ),
        )
    return store


def _signal(**kw) -> TradeSignal:
    base = dict(
        symbol="TEST",
        timeframe="1d",
        pattern="pattern_003_double_bottom",
        action="BUY",
        price=100.0,
        confidence=0.9,
        qty=1,
    )
    base.update(kw)
    return TradeSignal(**base)


class _FakePredictor:
    def __init__(self, pred_close: float):
        self.pred_close = pred_close
        self.last_kwargs: dict | None = None

    def predict(self, **kwargs):
        self.last_kwargs = kwargs
        # One row per forecast day; gate reads last close of the horizon.
        pred_len = kwargs.get("pred_len", 3)
        closes = [self.pred_close] * pred_len
        return pd.DataFrame({"close": closes})


def test_gate_pass_aligned_buy():
    gate = KronosGate()
    fake = _FakePredictor(110.0)  # +10%
    gate._predictor = fake
    store = _fill_store()
    sig = _signal(action="BUY")
    result = gate.check(sig, store, adjust_exits=True)
    assert result.passed, result
    assert result.pred_1w is not None and result.pred_1w > 0
    assert abs(sig.take_profit - 110.0) < 1e-6
    assert "KronosGate" in sig.notes
    # Official-shaped inputs: lookback rows + amount column.
    assert fake.last_kwargs is not None
    assert len(fake.last_kwargs["df"]) == _context_lookback()
    assert "amount" in fake.last_kwargs["df"].columns
    assert _context_lookback() == LOOKBACK


def test_gate_reject_wrong_direction():
    gate = KronosGate()
    gate._predictor = _FakePredictor(90.0)  # -10%, conflicts with BUY
    store = _fill_store()
    result = gate.check(_signal(action="BUY"), store, adjust_exits=False)
    assert not result.passed
    assert "conflicts" in result.reason


def test_gate_reject_small_move():
    gate = KronosGate()
    gate._predictor = _FakePredictor(101.0)  # +1% < default 3%
    store = _fill_store()
    result = gate.check(_signal(action="BUY"), store, adjust_exits=False)
    assert not result.passed
    assert "min" in result.reason


def test_gate_pass_three_pct():
    gate = KronosGate()
    gate._predictor = _FakePredictor(103.0)  # +3% meets default floor
    store = _fill_store()
    result = gate.check(_signal(action="BUY"), store, adjust_exits=False)
    assert result.passed, result


def test_gate_skips_close():
    gate = KronosGate()
    # Would fail if predictor were consulted — leave it unloaded.
    result = gate.check(
        _signal(action="CLOSE"),
        _fill_store(),
    )
    assert result.passed and result.reason == "skipped"


def test_gate_fail_closed_no_weights():
    gate = KronosGate()
    # No predictor, MODEL_PATH may or may not exist — force missing path behavior
    # by marking load failed after ensuring we don't try real load.
    gate._load_failed = True
    result = gate.check(_signal(), _fill_store())
    assert not result.passed
    assert "fail-closed" in result.reason


def test_gate_prefers_history_facade(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()
    idx = pd.bdate_range("2024-01-02", periods=LOOKBACK)
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=idx,
    )
    monkeypatch.setattr(
        "data.history.load_daily_ohlcv_df", lambda *_a, **_k: df,
    )
    gate = KronosGate()
    gate._predictor = _FakePredictor(110.0)
    empty = OHLCVStore(window=DEFAULT_WINDOW)
    result = gate.check(_signal(action="BUY"), empty, adjust_exits=False)
    assert result.passed, result
    kg._facade_df_cache.clear()


if __name__ == "__main__":
    test_gate_pass_aligned_buy()
    test_gate_reject_wrong_direction()
    test_gate_reject_small_move()
    test_gate_pass_three_pct()
    test_gate_skips_close()
    test_gate_fail_closed_no_weights()
    print("kronos_gate tests OK")
