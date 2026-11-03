import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.backtester import _check_exit, _open_trade
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _candle(close=100.0, ts=datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)):
    return OHLCVCandle(
        open=close, high=close + 1, low=close - 1, close=close, volume=1000,
        timestamp=ts,
    )


def _position(action="BUY", symbol="TEST"):
    return SimpleNamespace(
        symbol=symbol, action=action, timeframe="1d", pattern="test",
        pending_exit_reason="", kronos_exit_evidence=None,
    )


class _FakePaper:
    def __init__(self, positions):
        self.positions = positions
        self.requests = []

    def request_exit(self, position, reason="kronos_exit"):
        self.requests.append((position, reason))
        position.pending_exit_reason = reason
        return True


def _vote(*returns):
    def fetch(symbols):
        return {symbol: tuple(returns) for symbol in symbols}
    return fetch


def _scanner(exit_mode, fetch, positions):
    scanner = MarketScanner(
        symbols=["TEST"], market="us", volume_gate=False,
        kronos_gate_mode="off", kronos_exit_mode=exit_mode,
    )
    scanner._store.get_df = Mock(return_value=None)
    scanner._paper = _FakePaper({"TEST": positions})
    if scanner._kronos_gate is not None:
        scanner._kronos_gate._fetch_fn = fetch
    return scanner


@pytest.mark.parametrize("action,returns", [
    ("BUY", (-0.01, -0.02, 0.01)),
    ("SELL", (0.01, 0.02, -0.01)),
])
def test_oppose_queues_a_next_open_exit(action, returns):
    position = _position(action)
    scanner = _scanner("exit", _vote(*returns), [position])
    scanner._queue_kronos_exits("TEST", "1d", _candle())
    assert len(scanner._kronos_exit_pending) == 1

    asyncio.run(scanner._flush_kronos_confirm())

    assert scanner._paper.requests == [(position, "kronos_exit")]
    assert position.pending_exit_reason == "kronos_exit"
    assert position.kronos_exit_evidence["decision"] == "oppose"
    assert scanner.stats["kronos_exits"] == 1


@pytest.mark.parametrize("returns", [
    (0.01, 0.02, -0.01),   # confirms a long
    (0.01, 0.0, -0.01),    # no opinion
])
def test_confirm_and_no_opinion_hold(returns):
    position = _position("BUY")
    scanner = _scanner("exit", _vote(*returns), [position])
    scanner._queue_kronos_exits("TEST", "1d", _candle())
    asyncio.run(scanner._flush_kronos_confirm())
    assert scanner._paper.requests == []
    assert position.pending_exit_reason == ""
    assert scanner.stats["kronos_exits"] == 0


def test_shadow_records_without_exiting():
    position = _position("BUY")
    scanner = _scanner("shadow", _vote(-0.01, -0.02, 0.01), [position])
    scanner._queue_kronos_exits("TEST", "1d", _candle())
    asyncio.run(scanner._flush_kronos_confirm())
    assert scanner._paper.requests == []
    assert position.kronos_exit_evidence["mode"] == "shadow"
    assert position.kronos_exit_evidence["decision"] == "oppose"
    assert scanner.stats["kronos_exit_shadow"] == 1


def test_off_never_calls_the_worker():
    position = _position("BUY")
    fetch = Mock(return_value={"TEST": (-0.01, -0.02, 0.01)})
    scanner = _scanner("off", fetch, [position])
    scanner._queue_kronos_exits("TEST", "1d", _candle())
    asyncio.run(scanner._flush_kronos_confirm())
    assert scanner._kronos_exit_pending == []
    fetch.assert_not_called()
    assert position.kronos_exit_evidence is None


def test_paper_request_exit_fills_at_the_next_open():
    candle = _candle()
    signal = TradeSignal(
        symbol="TEST", action="BUY", pattern="test", timeframe="1d",
        confidence=0.8, price=100.0, qty=10, stop_loss=97.0, take_profit=110.0,
    )
    position = _open_trade(signal, candle, 0, execution_model="causal-v1")
    account = PaperAccount(market="us")
    account.positions["TEST"] = [position]

    assert account.request_exit(position) is True
    assert position.pending_exit_reason == "kronos_exit"

    next_bar = _candle(99.0, datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc))
    price, reason = _check_exit(next_bar, position, 1)
    assert price == next_bar.open
    assert reason == "kronos_exit"


def test_request_exit_refuses_a_closed_position():
    account = PaperAccount(market="us")
    position = _position()
    assert account.request_exit(position) is False


def test_request_exit_does_not_overwrite_an_existing_exit():
    account = PaperAccount(market="us")
    position = _position()
    position.pending_exit_reason = "stop_loss"
    account.positions["TEST"] = [position]
    assert account.request_exit(position) is True
    assert position.pending_exit_reason == "stop_loss"


def test_exit_gate_does_not_defer_entries_when_entry_mode_is_off():
    scanner = _scanner("exit", _vote(-0.01, -0.02, 0.01), [])
    scanner._queue_entry = AsyncMock()
    signal = TradeSignal(
        symbol="TEST", action="BUY", pattern="test", timeframe="1d",
        confidence=0.8, price=100.0, qty=10, stop_loss=97.0, take_profit=110.0,
    )
    asyncio.run(scanner._finish_signal(signal, None, _candle()))
    scanner._queue_entry.assert_awaited_once()


def test_exit_log_row_records_the_kronos_vote(monkeypatch):
    monkeypatch.setattr("core.scanner.append_signal_log", lambda *a, **k: None)
    scanner = _scanner("off", None, [])
    scanner._signal_log.clear()
    position = SimpleNamespace(
        symbol="TEST", timeframe="1d", action="BUY", pattern="test",
        confidence=0.8, exit_price=99.0, exit_reason="kronos_exit",
        sim_exit_date=datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc),
        exit_bar_idx=3, chart_annotations=[], kronos_evidence={"decision": "confirm"},
        kronos_exit_evidence={"decision": "oppose", "reason": "3/3 forecasts down"},
    )
    scanner._append_exit_log(position)
    row = scanner.signal_log_snapshot()[-1]
    assert row["status"] == "exited"
    assert row["exit_reason"] == "kronos_exit"
    assert row["kronos_exit_evidence"]["decision"] == "oppose"
    assert "Kronos exit" in row["reason"]
    assert row["price"] == 99.0
