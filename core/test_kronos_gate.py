"""Unit checks for core/kronos_gate without loading real Kronos weights."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from config import Settings
from core.kronos_eval import GATE_HORIZON_BARS, LOOKBACK, WEEK_AHEAD
from core.kronos_gate import KronosGate, _context_lookback
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _fill_store(n: int | None = None, last_close: float = 100.0, symbol: str = "TEST") -> OHLCVStore:
    n = max(LOOKBACK, _context_lookback()) if n is None else n
    store = OHLCVStore(window=DEFAULT_WINDOW)
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    for i in range(n):
        # Flat path ending at last_close — gate only cares about forecast vs close.
        c = last_close
        store.append_candle(
            symbol,
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
        self.batch_calls: list[int] = []

    def predict(self, **kwargs):
        self.last_kwargs = kwargs
        # One row per forecast day; gate reads last close of the horizon.
        pred_len = kwargs.get("pred_len", 3)
        closes = [self.pred_close] * pred_len
        return pd.DataFrame({"close": closes})

    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, **kwargs):
        self.batch_calls.append(len(df_list))
        self.last_kwargs = {
            "df_list": df_list,
            "x_timestamp_list": x_timestamp_list,
            "y_timestamp_list": y_timestamp_list,
            "pred_len": pred_len,
            **kwargs,
        }
        return [
            pd.DataFrame({"close": [self.pred_close] * pred_len})
            for _ in df_list
        ]


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
    assert fake.last_kwargs["pred_len"] == GATE_HORIZON_BARS == 3
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
    assert "pred_3d" in result.reason
    assert "in 3d" in result.reason


def test_gate_pass_three_pct():
    gate = KronosGate()
    gate._predictor = _FakePredictor(103.0)  # +3% meets default floor
    store = _fill_store()
    result = gate.check(_signal(action="BUY"), store, adjust_exits=False)
    assert result.passed, result
    assert result.pred_1w is not None
    assert abs(result.pred_1w - 0.03) < 1e-9


def test_gate_contract_is_three_pct_in_three_days():
    assert GATE_HORIZON_BARS == WEEK_AHEAD == 3
    assert Settings.model_fields["kronos_min_move_pct"].default == 0.03
    assert Settings.model_fields["kronos_batch_enabled"].default is False


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


def test_gate_uses_store_without_hitting_api(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()

    def _boom(_symbol: str):
        raise AssertionError("Kronos gate must use scan/replay store, not re-fetch 33ai")

    monkeypatch.setattr(kg, "_facade_daily_df", _boom)
    gate = KronosGate()
    gate._predictor = _FakePredictor(110.0)
    result = gate.check(_signal(action="BUY"), _fill_store(), adjust_exits=False)
    assert result.passed, result
    kg._facade_df_cache.clear()


def _store_with(*symbols: str) -> OHLCVStore:
    store = OHLCVStore(window=DEFAULT_WINDOW)
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    n = max(LOOKBACK, _context_lookback())
    for symbol in symbols:
        for i in range(n):
            store.append_candle(
                symbol, "1d",
                OHLCVCandle(
                    open=100.0, high=101.0, low=99.0, close=100.0,
                    volume=1_000_000.0, timestamp=start + timedelta(days=i),
                ),
            )
    return store


def test_check_many_batches_unique_symbols(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()
    monkeypatch.setattr(kg, "_facade_daily_df", lambda _s: None)
    gate = KronosGate()
    fake = _FakePredictor(110.0)
    gate._predictor = fake
    store = _store_with("AAA", "BBB", "CCC")
    sigs = [
        _signal(symbol="AAA", action="BUY"),
        _signal(symbol="BBB", action="BUY"),
        _signal(symbol="CCC", action="BUY"),
    ]
    results = gate.check_many(sigs, store, adjust_exits=False)
    assert len(results) == 3
    assert all(r.passed for r in results)
    assert fake.batch_calls == [3]
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["pred_len"] == GATE_HORIZON_BARS == 3
    df_list = fake.last_kwargs["df_list"]
    x_ts_list = fake.last_kwargs["x_timestamp_list"]
    y_ts_list = fake.last_kwargs["y_timestamp_list"]
    seq = len(df_list[0])
    assert seq >= 60
    for df, x_ts, y_ts in zip(df_list, x_ts_list, y_ts_list):
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
        assert not df.isna().any().any()
        assert len(df) == seq == len(pd.Series(x_ts))
        assert len(pd.Series(y_ts)) == GATE_HORIZON_BARS


def test_check_many_dedupes_same_symbol(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()
    monkeypatch.setattr(kg, "_facade_daily_df", lambda _s: None)
    gate = KronosGate()
    fake = _FakePredictor(110.0)
    gate._predictor = fake
    store = _store_with("AAA")
    sigs = [
        _signal(symbol="AAA", pattern="pattern_003_double_bottom"),
        _signal(symbol="AAA", pattern="pattern_004_rounding_bottom"),
    ]
    results = gate.check_many(sigs, store, adjust_exits=False)
    assert len(results) == 2
    assert all(r.passed for r in results)
    assert fake.batch_calls == [1]


def test_check_many_groups_mixed_lookbacks(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()
    idx400 = pd.bdate_range("2024-01-02", periods=400)
    idx80 = pd.bdate_range("2024-01-02", periods=80)
    long_df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=idx400,
    )
    short_df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=idx80,
    )

    def _facade(symbol: str):
        return long_df if symbol == "LONG" else short_df

    monkeypatch.setattr(kg, "_facade_daily_df", _facade)
    gate = KronosGate()
    fake = _FakePredictor(110.0)
    gate._predictor = fake
    empty = OHLCVStore(window=DEFAULT_WINDOW)
    results = gate.check_many(
        [_signal(symbol="LONG"), _signal(symbol="SHORT")],
        empty, adjust_exits=False,
    )
    assert len(results) == 2
    assert all(r.passed for r in results)
    assert sorted(fake.batch_calls) == [1, 1]


def test_check_many_short_frame_fail_closed(monkeypatch):
    from core import kronos_gate as kg

    kg._facade_df_cache.clear()
    idx = pd.bdate_range("2024-01-02", periods=20)
    short = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=idx,
    )
    long = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=pd.bdate_range("2024-01-02", periods=400),
    )

    def _facade(symbol: str):
        return long if symbol == "OK" else short

    monkeypatch.setattr(kg, "_facade_daily_df", _facade)
    gate = KronosGate()
    fake = _FakePredictor(110.0)
    gate._predictor = fake
    empty = OHLCVStore(window=DEFAULT_WINDOW)
    results = gate.check_many(
        [_signal(symbol="OK"), _signal(symbol="THIN")],
        empty, adjust_exits=False,
    )
    assert results[0].passed
    assert not results[1].passed
    assert "fail-closed" in results[1].reason
    assert fake.batch_calls == [1]


def _ohlcv_df(n: int, close: float = 100.0, *, tz: str | None = None) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=n, tz=tz)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=idx,
    )


def test_batch_payload_rejects_nan_without_poisoning_neighbors():
    from core.kronos_eval import predict_1w_return_batch

    fake = _FakePredictor(110.0)
    good = _ohlcv_df(LOOKBACK)
    bad = good.copy()
    bad.iloc[-1, bad.columns.get_loc("close")] = float("nan")
    outs = predict_1w_return_batch(
        fake, [good, bad, good.copy()], sample_count=1, lookback=LOOKBACK,
    )
    assert outs[0] is not None and outs[2] is not None
    assert outs[1] is None
    assert fake.batch_calls == [2]
    for df in fake.last_kwargs["df_list"]:
        assert not df.isna().any().any()
        assert len(df) == len(fake.last_kwargs["x_timestamp_list"][0])


def test_batch_payload_tz_aware_index_is_naive_datetime():
    from core.kronos_eval import predict_1w_return_batch

    fake = _FakePredictor(110.0)
    df = _ohlcv_df(LOOKBACK, tz="America/New_York")
    outs = predict_1w_return_batch(fake, [df], sample_count=1, lookback=LOOKBACK)
    assert outs[0] is not None
    x_ts = pd.Series(fake.last_kwargs["x_timestamp_list"][0])
    assert getattr(x_ts.dt, "tz", None) is None
    y_ts = pd.Series(fake.last_kwargs["y_timestamp_list"][0])
    assert len(y_ts) == GATE_HORIZON_BARS


def test_cpu_caps_kronos_batch_size(monkeypatch):
    from core import kronos_eval as ke

    monkeypatch.setattr(ke, "_cuda_available", lambda: False)
    ke._LOGGED_CPU_BATCH_CAP = False
    assert ke.effective_kronos_batch_size(16) == ke.CPU_KRONOS_BATCH_CAP
    assert ke.effective_kronos_batch_size(2) == 2
    monkeypatch.setattr(ke, "_cuda_available", lambda: True)
    assert ke.effective_kronos_batch_size(16) == 16


def test_cpu_batch_chunks_at_cap(monkeypatch):
    from core import kronos_eval as ke
    from core.kronos_eval import predict_1w_return_batch

    monkeypatch.setattr(ke, "_cuda_available", lambda: False)
    ke._LOGGED_CPU_BATCH_CAP = False
    fake = _FakePredictor(110.0)
    frames = [_ohlcv_df(LOOKBACK) for _ in range(9)]
    outs = predict_1w_return_batch(
        fake, frames, sample_count=1, lookback=LOOKBACK, batch_size=16,
    )
    assert all(o is not None for o in outs)
    assert fake.batch_calls == [4, 4, 1]


if __name__ == "__main__":
    test_gate_pass_aligned_buy()
    test_gate_reject_wrong_direction()
    test_gate_reject_small_move()
    test_gate_pass_three_pct()
    test_gate_contract_is_three_pct_in_three_days()
    test_gate_skips_close()
    test_gate_fail_closed_no_weights()
    print("kronos_gate tests OK")
