"""Pattern-short permissions cover queues, direct fills and restarted orders."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from config import Settings, settings
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from utils.trade_export import _book_export, build_paper_account_export


def account() -> PaperAccount:
    result = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=0)
    result.assume_session_open = True
    return result


def test_us_pattern_shorts_are_enabled_by_default():
    assert Settings.model_fields["short_entries_enabled"].default is True


@pytest.mark.parametrize("entry_path", ["queue_position", "open_position"])
def test_disabled_short_cannot_bypass_scanner(monkeypatch, entry_path):
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    book = account()
    ok, reason = getattr(book, entry_path)(signal(True), candle(100, 100, 100, 100), OHLCVStore())
    assert not ok and "SHORT_ENTRIES_ENABLED=false" in reason
    assert not book.pending_entries and not book.consumed_anchors
    assert book.open_count() == 0 and book.cash == book.initial_capital


def test_saved_short_order_is_rechecked_without_charging_fees(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    book = account()
    store = OHLCVStore()
    assert book.queue_position(signal(True), candle(100, 100, 100, 100), store)[0]
    path = tmp_path / "book.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    book.assume_session_open = True
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    outcomes = book.fill_pending("TEST", "1d", candle(100, 101, 99, 100, 1), store)
    assert len(outcomes) == 1 and outcomes[0][1] is False
    assert "Short entries are disabled" in outcomes[0][2]
    assert not book.pending_entries and book.open_count() == 0
    assert book.cash == book.initial_capital
    assert book.order_events[-1]["status"] == "rejected"


def test_disabled_entries_leave_existing_short_protection_active(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    book = account()
    assert book.open_position(signal(True), candle(100, 100, 100, 100), OHLCVStore())[0]
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    closed = book.on_bar("TEST", candle(110, 112, 109, 111, 1), "1d")
    assert len(closed) == 1 and closed[0].exit_reason == "stop_loss"
    assert closed[0].exit_price == 110
    assert book.cash == pytest.approx(book.initial_capital + closed[0].pnl_usd)


@pytest.mark.parametrize("short,indicators_only,enabled", [
    (False, False, False), (True, False, True), (True, True, False),
])
def test_allowed_entries_still_fill(monkeypatch, short, indicators_only, enabled):
    monkeypatch.setattr(settings, "short_entries_enabled", enabled)
    book = account()
    book.indicators_only = indicators_only
    assert book.open_position(signal(short), candle(100, 100, 100, 100), OHLCVStore())[0]
    assert book.open_count() == 1


def test_scanner_uses_the_same_permission(monkeypatch):
    monkeypatch.setattr(settings, "short_entries_enabled", False)
    scanner = MarketScanner(symbols=["TEST"], market="us", volume_gate=False)
    scanner._finish_signal = AsyncMock()
    scanner._append_signal_log = Mock()
    asyncio.run(scanner._process_signal(signal(True)))
    scanner._finish_signal.assert_not_awaited()
    assert scanner.stats["short_rejected"] == 1


def test_cli_and_ui_export_actual_execution_context(monkeypatch):
    from core.paper_books import PaperBook

    monkeypatch.setattr(settings, "short_entries_enabled", False)
    monkeypatch.setattr("core.signal_log_store.load_signal_log", lambda *args, **kwargs: [])
    monkeypatch.setattr("core.paper_books.load_signal_log", lambda *args, **kwargs: [])
    book = account()
    book.slippage_pct = .002
    book.on_bar("TEST", candle(100, 100, 100, 100), "1d")
    cli = build_paper_account_export(book)["books"][0]["execution_context"]
    monkeypatch.setattr("core.paper_books.PaperAccount.load", lambda **kwargs: book)
    ui = _book_export(PaperBook("us").snapshot())["execution_context"]
    assert ui == cli
    assert cli["txn_cost_pct_per_side"] == .001
    assert cli["slippage_pct_per_side"] == .002
    assert cli["short_entries_enabled"] is False
    assert cli["sim_as_of"] == candle(100, 100, 100, 100).timestamp.isoformat()
    assert cli["initial_capital"] == book.initial_capital
    assert "historical rules" in cli["scope"]
    assert _book_export({"market": "us"})["execution_context"] is None
