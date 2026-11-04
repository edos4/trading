import asyncio
from unittest.mock import AsyncMock, Mock

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


def _pattern_scanner() -> MarketScanner:
    scanner = MarketScanner(symbols=["TEST"], market="us", volume_gate=False)
    scanner._finish_signal = AsyncMock()
    scanner._append_signal_log = Mock()
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


def test_fixed_stop_puts_the_trail_in_r(monkeypatch):
    monkeypatch.setattr(settings, "stop_loss_pct", 0.03)
    monkeypatch.setattr(settings, "stop_loss_trail_r", 2.0)
    monkeypatch.setattr(settings, "stop_loss_arm_r", 2.0)
    scanner = _scanner()
    signal = _signal(action="BUY")
    scanner._apply_fixed_stop(signal, None)
    assert signal.trailing_stop_pct == pytest.approx(0.06)
    assert signal.trailing_activation_pct == pytest.approx(0.06)
    assert signal.trailing_stop_mode == "highest_close"


def test_short_entries_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    scanner = _pattern_scanner()
    signal = _signal(action="SELL")
    asyncio.run(scanner._process_signal(signal))
    scanner._finish_signal.assert_not_awaited()
    assert "Short entries are disabled" in scanner._append_signal_log.call_args.kwargs["reason"]
    assert scanner.stats["short_rejected"] == 1


def test_price_floor_is_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    monkeypatch.setattr(settings, "min_signal_price_usd", 10.0)
    scanner = _pattern_scanner()
    signal = _signal(price=5.0)
    asyncio.run(scanner._process_signal(signal))
    scanner._finish_signal.assert_not_awaited()
    assert "below the 10.00 minimum" in scanner._append_signal_log.call_args.kwargs["reason"]
    assert scanner.stats["price_floor_rejected"] == 1


def test_dii_shadow_records_without_blocking(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    monkeypatch.setattr(settings, "min_signal_price_usd", 0.0)
    monkeypatch.setattr(settings, "dii_gate_shadow", True)
    monkeypatch.setattr(
        "analysis.pattern_dii.describe_pattern_dii_rejection",
        lambda df, action=None: "DII bullish percentage 90.00% outside required 17%–40%.",
    )
    scanner = _pattern_scanner()
    scanner._store.get_df = Mock(return_value=None)
    signal = _signal(action="BUY")
    asyncio.run(scanner._process_signal(signal))
    scanner._finish_signal.assert_awaited_once()
    assert scanner.stats["dii_shadow"] == 1
    assert scanner._append_signal_log.call_args.kwargs["status"] == "shadow"
