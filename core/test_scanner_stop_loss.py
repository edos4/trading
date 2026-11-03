import asyncio
from unittest.mock import AsyncMock

import pytest

from config import settings
from core.scanner import MarketScanner
from patterns.base_pattern import TradeSignal


def _scanner() -> MarketScanner:
    scanner = MarketScanner(
        symbols=["TEST"], market="us", indicators_only=True, volume_gate=False,
    )
    scanner._finish_signal = AsyncMock()
    return scanner


def _signal(action="BUY", price=100.0, stop_loss=90.0) -> TradeSignal:
    return TradeSignal(
        symbol="TEST", action=action, pattern="test", timeframe="1d",
        confidence=0.8, price=price, qty=10, stop_loss=stop_loss,
        take_profit=130.0,
    )


@pytest.mark.parametrize("action,expected", [("BUY", 97.0), ("SELL", 103.0)])
def test_every_entry_gets_the_configured_stop(monkeypatch, action, expected):
    monkeypatch.setattr(settings, "stop_loss_pct", 0.03)
    scanner = _scanner()
    signal = _signal(action=action)
    asyncio.run(scanner._process_signal(signal))
    assert signal.stop_loss == pytest.approx(expected)
    assert signal.stop_loss_pct_cap == pytest.approx(0.03)
    scanner._finish_signal.assert_awaited_once()


def test_stop_override_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "stop_loss_pct", 0.0)
    scanner = _scanner()
    signal = _signal()
    asyncio.run(scanner._process_signal(signal))
    assert signal.stop_loss == 90.0


def test_close_signals_keep_their_levels(monkeypatch):
    monkeypatch.setattr(settings, "stop_loss_pct", 0.03)
    scanner = _scanner()
    signal = _signal(action="CLOSE")
    asyncio.run(scanner._process_signal(signal))
    assert signal.stop_loss == 90.0


def test_stop_falls_back_to_the_candle_close(monkeypatch):
    monkeypatch.setattr(settings, "stop_loss_pct", 0.03)
    scanner = _scanner()
    signal = _signal(price=0.0)

    class Candle:
        close = 50.0

    scanner._apply_fixed_stop(signal, Candle())
    assert signal.stop_loss == 48.5
