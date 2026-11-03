import math
from types import SimpleNamespace

import pandas as pd
import pytest

from core.kronos_confirm import (
    KronosConfirmGate,
    atr_pct_from_frame,
    classify,
    evaluate,
    sideways_band_pct,
    summarize,
)
from core.kronos_process import KronosUnavailable
from patterns.base_pattern import TradeSignal


def _signal(action="BUY", symbol="TEST"):
    return TradeSignal(
        symbol=symbol, action=action, pattern="test", timeframe="1d",
        confidence=0.8, price=100.0, qty=10, stop_loss=95.0, take_profit=110.0,
    )


def _candle(close=100.0, ts="2026-10-30"):
    return SimpleNamespace(close=close, timestamp=pd.Timestamp(ts, tz="UTC"))


def _verdict(returns, action="BUY", *, min_agree=2, band=0.56):
    return evaluate(summarize(100.0, band, returns), action, min_agree=min_agree)


def test_band_clamps_at_both_ends():
    assert sideways_band_pct(0.05) == pytest.approx(0.25)
    assert sideways_band_pct(9.0) == pytest.approx(3.0)
    assert sideways_band_pct(1.60) == pytest.approx(0.56)


def test_atr_pct_from_frame():
    frame = pd.DataFrame({
        "high": [101.0] * 40,
        "low": [99.0] * 40,
        "close": [100.0] * 40,
    })
    assert atr_pct_from_frame(frame, 100.0) == pytest.approx(2.0)
    assert atr_pct_from_frame(None, 100.0) == 0.0


def test_classify_exact_boundaries():
    assert classify(0.0056, 0.56) == "up"
    assert classify(-0.0056, 0.56) == "down"
    assert classify(0.005599, 0.56) == "flat"
    assert classify(-0.005599, 0.56) == "flat"


def test_vote_tally():
    summary = summarize(100.0, 0.56, (0.01, -0.01, 0.0))
    assert (summary.up, summary.down, summary.flat) == (1, 1, 1)
    assert summary.mean_return == pytest.approx(0.0)


@pytest.mark.parametrize("returns,action,expected", [
    ((0.01, 0.02, -0.01), "BUY", "confirm"),
    ((0.01, 0.02, 0.0), "BUY", "confirm"),
    ((-0.01, -0.02, 0.01), "BUY", "oppose"),
    ((-0.01, -0.02, 0.0), "BUY", "oppose"),
    ((0.01, 0.0, -0.01), "BUY", "no_opinion"),
    ((0.01, 0.0, 0.0), "BUY", "no_opinion"),
    ((0.0, 0.0, 0.0), "BUY", "no_opinion"),
    ((-0.01, -0.02, 0.01), "SELL", "confirm"),
    ((0.01, 0.02, -0.01), "SELL", "oppose"),
])
def test_three_vote_truth_table(returns, action, expected):
    assert _verdict(returns, action).decision == expected


def test_min_agree_ties_withhold_opinion():
    assert _verdict((0.01, 0.02, 0.0), min_agree=3).decision == "no_opinion"
    assert _verdict((0.01, 0.02, -0.01), min_agree=2).decision == "confirm"


def test_too_few_usable_simulations_is_no_opinion():
    verdict = _verdict((0.01,), min_agree=2)
    assert verdict.decision == "no_opinion"
    assert "usable" in verdict.reason


def test_cache_hit_and_miss():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    gate = KronosConfirmGate(mode="shadow", samples=3, cache_minutes=30, fetch_fn=fetch)
    signal = _signal()
    candle = _candle()

    gate.queue(signal, candle, None)
    first = gate.flush()
    assert calls == [["TEST"]]
    assert first[0][2].decision == "confirm"
    assert first[0][3]["cached"] is False

    gate.queue(signal, candle, None)
    second = gate.flush()
    assert calls == [["TEST"]]
    assert second[0][3]["cached"] is True

    gate.queue(signal, _candle(ts="2026-11-02"), None)
    gate.flush()
    assert calls == [["TEST"], ["TEST"]]


def test_cache_expires():
    now = [0.0]

    def fetch(symbols):
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    gate = KronosConfirmGate(
        mode="shadow", cache_minutes=1, fetch_fn=fetch, now_fn=lambda: now[0],
    )
    signal = _signal()
    candle = _candle()
    gate.queue(signal, candle, None)
    gate.flush()
    now[0] = 61.0
    gate.queue(signal, candle, None)
    result = gate.flush()
    assert result[0][3]["cached"] is False


def test_unavailable_is_no_opinion():
    def fetch(symbols):
        raise KronosUnavailable("no .venv-kronos")

    gate = KronosConfirmGate(mode="confirm", fetch_fn=fetch)
    gate.queue(_signal(), _candle(), None)
    rows = gate.flush()
    assert rows[0][2].decision == "no_opinion"
    assert "unavailable" in rows[0][3]["reason"].lower()


def test_over_cap_symbols_vote_no_opinion():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    gate = KronosConfirmGate(mode="confirm", max_checks_per_scan=1, fetch_fn=fetch)
    gate.queue(_signal(symbol="AAA"), _candle(), None)
    gate.queue(_signal(symbol="BBB"), _candle(), None)
    rows = gate.flush()
    assert calls == [["AAA"]]
    decisions = {row[0].symbol: row[2].decision for row in rows}
    assert decisions == {"AAA": "confirm", "BBB": "no_opinion"}
    assert math.isfinite(rows[0][3]["atr_pct"])


def _position(action="BUY", symbol="TEST"):
    return SimpleNamespace(symbol=symbol, action=action, timeframe="1d")


@pytest.mark.parametrize("action,returns,expected", [
    ("BUY", (-0.01, -0.02, 0.01), "oppose"),
    ("BUY", (0.01, 0.02, -0.01), "confirm"),
    ("SELL", (0.01, 0.02, -0.01), "oppose"),
    ("SELL", (-0.01, -0.02, 0.01), "confirm"),
    ("BUY", (0.01, 0.0, -0.01), "no_opinion"),
])
def test_open_position_votes(action, returns, expected):
    def fetch(symbols):
        return {symbol: returns for symbol in symbols}

    gate = KronosConfirmGate(mode="off", exit_mode="exit", fetch_fn=fetch)
    position = _position(action)
    gate.prepare_exit([position], _candle(), None)
    entries, exits = gate.flush_with_exits()
    assert entries == []
    assert len(exits) == 1
    assert exits[0][0] is position
    assert exits[0][1].decision == expected
    assert exits[0][2]["scope"] == "position"
    assert exits[0][2]["mode"] == "exit"


def test_exit_mode_off_queues_nothing():
    gate = KronosConfirmGate(mode="off", exit_mode="off")
    assert gate.prepare_exit([_position()], _candle(), None) is None
    assert gate.flush_with_exits() == ([], [])


def test_entry_and_exit_share_one_worker_call():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    gate = KronosConfirmGate(
        mode="confirm", exit_mode="exit", fetch_fn=fetch, cache_minutes=0,
    )
    gate.queue(_signal(symbol="AAA"), _candle(), None)
    gate.prepare_exit([_position(symbol="BBB")], _candle(), None)
    entries, exits = gate.flush_with_exits()
    assert calls == [["AAA", "BBB"]]
    assert len(entries) == 1 and len(exits) == 1
