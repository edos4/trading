import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from core.kronos_process import KronosBusy, KronosError, KronosUnavailable
from core.scanner import MarketScanner
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


@pytest.fixture(autouse=True)
def _no_signal_log_io(monkeypatch):
    monkeypatch.setattr("core.scanner.append_signal_log", lambda *a, **k: None)


def _candle(close=100.0) -> OHLCVCandle:
    return OHLCVCandle(
        open=close, high=close + 1, low=close - 1, close=close, volume=1000,
        timestamp=datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc),
    )


def _signal(symbol="TEST", action="BUY") -> TradeSignal:
    return TradeSignal(
        symbol=symbol, action=action, pattern="test", timeframe="1d",
        confidence=0.8, price=100.0, qty=10, stop_loss=95.0, take_profit=110.0,
    )


def _scanner(mode, fetch=None, samples=3):
    scanner = MarketScanner(
        symbols=["TEST"], market="us", volume_gate=False,
        kronos_gate_mode=mode, kronos_gate_samples=samples,
    )
    scanner._store.get_df = Mock(return_value=None)
    scanner._append_signal_log = Mock(wraps=scanner._append_signal_log)
    scanner._queue_entry = AsyncMock()
    if fetch is not None:
        scanner._kronos_gate._fetch_fn = fetch
    return scanner


def _vote(*returns):
    def fetch(symbols):
        return {symbol: tuple(returns) for symbol in symbols}
    return fetch


def test_off_constructs_no_gate_and_queues_directly():
    scanner = _scanner("off")
    assert scanner._kronos_gate is None
    signal = _signal()
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    scanner._queue_entry.assert_awaited_once()
    asyncio.run(scanner._flush_kronos_confirm())
    assert signal.kronos_evidence is None


@pytest.mark.parametrize("returns", [
    (0.01, 0.02, -0.01),   # confirm
    (-0.01, -0.02, 0.01),  # oppose
    (0.01, 0.0, -0.01),    # no_opinion
])
def test_shadow_never_blocks_and_stamps_evidence(returns):
    scanner = _scanner("shadow", fetch=_vote(*returns))
    signal = _signal()
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    scanner._queue_entry.assert_not_awaited()
    asyncio.run(scanner._flush_kronos_confirm())
    scanner._queue_entry.assert_awaited_once()
    assert signal.kronos_evidence["mode"] == "shadow"
    assert signal.kronos_evidence["decision"] in ("confirm", "oppose", "no_opinion")
    assert len(signal.kronos_evidence["returns"]) == 3


def test_confirm_queues_only_a_confirming_vote():
    scanner = _scanner("confirm", fetch=_vote(0.01, 0.02, -0.01))
    signal = _signal()
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    scanner._queue_entry.assert_not_awaited()
    asyncio.run(scanner._flush_kronos_confirm())
    scanner._queue_entry.assert_awaited_once()
    assert signal.kronos_evidence["decision"] == "confirm"


@pytest.mark.parametrize("returns", [
    (-0.01, -0.02, 0.01),  # oppose
    (0.01, 0.0, -0.01),    # no_opinion
])
def test_confirm_blocks_oppose_and_no_opinion(returns):
    scanner = _scanner("confirm", fetch=_vote(*returns))
    signal = _signal()
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    asyncio.run(scanner._flush_kronos_confirm())
    scanner._queue_entry.assert_not_awaited()
    scanner._append_signal_log.assert_called_once()
    kwargs = scanner._append_signal_log.call_args.kwargs
    assert kwargs["status"] == "rejected"
    assert kwargs["reason"].startswith("Kronos")
    assert scanner.stats["kronos_rejected"] == 1
    assert scanner.stats["rejection_by_gate"]["kronos"] == 1


@pytest.mark.parametrize("exc", [
    KronosUnavailable("no venv"),
    KronosBusy("busy"),
    KronosError("timed out"),
])
@pytest.mark.parametrize("mode,blocked", [("confirm", True), ("shadow", False)])
def test_failures_fail_closed_only_in_confirm(exc, mode, blocked):
    def fetch(symbols):
        raise exc

    scanner = _scanner(mode, fetch=fetch)
    signal = _signal()
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    asyncio.run(scanner._flush_kronos_confirm())
    if blocked:
        scanner._queue_entry.assert_not_awaited()
        assert scanner.stats["kronos_rejected"] == 1
    else:
        scanner._queue_entry.assert_awaited_once()
    assert signal.kronos_evidence["decision"] == "no_opinion"


def test_n_pending_signals_share_one_worker_call():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    scanner = _scanner("confirm", fetch=fetch)
    signals = [_signal(symbol) for symbol in ("AAA", "BBB", "CCC")]
    for signal in signals:
        asyncio.run(scanner._finish_signal(signal, None, _candle()))
    asyncio.run(scanner._flush_kronos_confirm())
    assert calls == [["AAA", "BBB", "CCC"]]
    assert scanner._queue_entry.await_count == 3
    assert all(s.kronos_evidence["cached"] is False for s in signals)


def test_several_patterns_on_one_symbol_share_one_vote():
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: (0.01, 0.02, -0.01) for symbol in symbols}

    scanner = _scanner("shadow", fetch=fetch)
    first = _signal()
    second = _signal()
    second.pattern = "other"
    asyncio.run(scanner._finish_signal(first, None, _candle()))
    asyncio.run(scanner._finish_signal(second, None, _candle()))
    asyncio.run(scanner._flush_kronos_confirm())
    assert calls == [["TEST"]]
    assert scanner._queue_entry.await_count == 2
    assert first.kronos_evidence["returns"] == second.kronos_evidence["returns"]
