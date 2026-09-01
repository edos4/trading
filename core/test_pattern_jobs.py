"""Off-loop pattern.analyze() jobs: private store, pickle contract, spawn pool."""

from __future__ import annotations

import pickle
import threading
from datetime import datetime, timedelta, timezone

from config import PATTERN_SCAN_HISTORY_BARS
from core import pattern_jobs as pj
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, TradeSignal


def _candle(close: float, ts: datetime) -> OHLCVCandle:
    return OHLCVCandle(open=close, high=close, low=close, close=close, volume=1.0, timestamp=ts)


def _snapshot(symbol: str, candles: list[OHLCVCandle]) -> MarketSnapshot:
    last = candles[-1]
    return MarketSnapshot(
        symbol=symbol, timeframe="1d", timestamp=last.timestamp,
        candle=last, indicators={}, summary={}, oscillators={}, moving_avgs={},
    )


class _OnceBuy(BasePattern):
    name = "test_pattern"

    @property
    def timeframes(self):
        return ["1d"]

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=2)
        if df is None:
            return None
        return TradeSignal(
            symbol=snapshot.symbol, action="BUY", pattern=self.name,
            timeframe="1d", confidence=0.9, price=snapshot.candle.close, qty=10,
            stop_loss=snapshot.candle.close * 0.90,
            take_profit=snapshot.candle.close * 1.20,
        )


def test_analyze_worker_count_inline_and_auto():
    assert pj.analyze_worker_count(1) == 1
    assert pj.analyze_worker_count(0) >= 2
    assert pj.analyze_worker_count(99) == 32


def test_copy_candles_is_a_snapshot():
    store = OHLCVStore(window=8)
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    store.apply_candle("AAPL", "1d", _candle(1.0, t0))
    copied = store.copy_candles("AAPL", "1d")
    assert len(copied) == 1
    store.apply_candle("AAPL", "1d", _candle(2.0, t0 + timedelta(days=1)))
    assert len(copied) == 1
    assert store.available("AAPL", "1d") == 2


def test_analyze_batch_private_store_emits_signal():
    prev_p, prev_s, prev_e = pj._worker_patterns, pj._worker_store, pj._worker_skip_edgar
    try:
        pj._worker_patterns = [_OnceBuy()]
        pj._worker_store = OHLCVStore(window=64)
        pj._worker_skip_edgar = True
        tz = timezone.utc
        candles = [
            _candle(90.0, datetime(2023, 11, 20, tzinfo=tz) + timedelta(days=i))
            for i in range(PATTERN_SCAN_HISTORY_BARS)
        ]
        snap = _snapshot("TEST", candles)
        n_eval, hits = pj.analyze_batch([(snap, candles)])[0]
        assert n_eval == 1
        assert len(hits) == 1
        assert hits[0].symbol == "TEST"
        assert hits[0].pattern == "test_pattern"
    finally:
        pj._worker_patterns = prev_p
        pj._worker_store = prev_s
        pj._worker_skip_edgar = prev_e


def test_analyze_job_pickle_roundtrip():
    tz = timezone.utc
    candles = [
        _candle(90.0, datetime(2023, 11, 20, tzinfo=tz) + timedelta(days=i))
        for i in range(PATTERN_SCAN_HISTORY_BARS)
    ]
    snap = _snapshot("AAPL", candles)
    snap2, c2 = pickle.loads(pickle.dumps((snap, candles)))
    assert snap2.symbol == "AAPL"
    assert len(c2) == PATTERN_SCAN_HISTORY_BARS
    signal = TradeSignal(
        symbol="AAPL", action="BUY", pattern="test_pattern",
        timeframe="1d", confidence=0.9, price=1.0, qty=1,
    )
    n_eval, hits = pickle.loads(pickle.dumps((1, [signal])))
    assert n_eval == 1
    assert hits[0].symbol == "AAPL"


def test_analyze_batch_spawn_pool_smoke():
    tz = timezone.utc
    candles = [
        _candle(90.0, datetime(2023, 11, 20, tzinfo=tz) + timedelta(days=i))
        for i in range(PATTERN_SCAN_HISTORY_BARS)
    ]
    snap = _snapshot("AAPL", candles)
    disabled = [p.name for p in pj.load_patterns([])]
    err: list[BaseException] = []

    def _run() -> None:
        try:
            pool = pj.make_analyze_pool(
                disabled=disabled,
                session_tz="America/New_York",
                skip_edgar=True,
                window=512,
                workers=2,
            )
            assert pool is not None
            try:
                results = pool.submit(pj.analyze_batch, [(snap, candles)]).result(timeout=60)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            assert len(results) == 1
            n_eval, hits = results[0]
            assert n_eval == 0
            assert hits == []
        except Exception as exc:
            err.append(exc)

    t = threading.Thread(target=_run, daemon=True, name="analyze-pool-smoke")
    t.start()
    t.join(timeout=60)
    assert not t.is_alive()
    if err:
        raise err[0]
