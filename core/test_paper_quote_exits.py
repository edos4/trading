"""Observed-price exits protect both sides without inventing intrabar paths."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from core.paper_trader import PaperAccount
from core.backtester import _make_snapshot
from core.scanner import MarketScanner
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle
from utils.trade_export import build_paper_account_export


@pytest.mark.parametrize("short", [False, True])
def test_forming_bar_stop_fills_at_observed_price_with_costs_and_settles_once(short):
    book, p = book_with_position(short, slip=.002)
    price = 104 if short else 96
    quote = candle(100, 120, 80, price, 1)
    now = p.entry_date + timedelta(seconds=5)
    count = book.bar_count("TEST", "1d")
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == [p]
    assert p.exit_reason == "stop_loss"
    assert p.exit_price == pytest.approx(price * (1.002 if short else .998))
    assert p.pnl_pct < -3  # Do not fabricate a fill at a crossed stop.
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    assert book.equity() == pytest.approx(book.cash)
    assert book.bar_count("TEST", "1d") == count
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == []
    trade = build_paper_account_export(book)["books"][0]["closed_trades"][0]
    check = trade["daily_marks"][-1]["exit_check"]
    assert check["observation_kind"] == "live_quote"
    assert check["raw_fill"] == price
    assert check["ohlc"] == dict.fromkeys(("open", "high", "low", "close"), price)


@pytest.mark.parametrize("short", [False, True])
def test_forming_range_and_profit_do_not_retroactively_trigger_or_ratchet_close_trail(short):
    book, p = book_with_position(short)
    original = deepcopy(p.paper_protection)
    marks = deepcopy(p.position_marks)
    now = p.entry_date + timedelta(seconds=5)
    quote = candle(100, 115, 85, 99 if short else 101, 1)
    assert book.on_quote("TEST", quote, "1d", observed_at=now) == []
    assert p.paper_protection == original
    assert p.position_marks == marks
    assert not p._trailing_activated


@pytest.mark.parametrize("short", [False, True])
def test_saved_trail_protects_forming_bar_after_restart(short, tmp_path):
    book, p = book_with_position(short)
    completed = candle(100, 101, 88, 90, 1) if short else candle(100, 112, 99, 110, 1)
    assert book.on_bar("TEST", completed, "1d") == []
    path = tmp_path / "account.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    p = book.positions["TEST"][0]
    stop = p.paper_protection["stop"]
    price = stop + .1 if short else stop - .1
    quote = candle(price, price, price, price, 2)
    assert book.on_quote("TEST", quote, "1d", observed_at=p.entry_date + timedelta(minutes=5)) == [p]
    assert p.exit_reason == "trailing_stop"
    assert p.pnl_pct > 6
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)


@pytest.mark.parametrize("short", [False, True])
def test_close_decision_waits_for_next_bar_then_fills_first_observed_price(short):
    book, p = book_with_position(short)
    assert book.request_exit(p, "kronos_exit")
    now = p.entry_date + timedelta(minutes=5)
    same = candle(100, 101, 99, 100)
    assert book.on_quote("TEST", same, "1d", observed_at=now) == []
    following = candle(100, 101, 99, 100, 1)
    assert book.on_quote("TEST", following, "1d", observed_at=now + timedelta(days=1)) == [p]
    assert p.exit_reason == "kronos_exit"
    assert p.exit_price == 100


@pytest.mark.parametrize("short", [False, True])
def test_wrong_timeframe_and_pre_entry_observations_cannot_close_a_position(short):
    book, p = book_with_position(short)
    quote = candle(100, 120, 80, 110 if short else 90, 1)
    assert book.on_quote("TEST", quote, "1W", observed_at=p.entry_date + timedelta(seconds=5)) == []
    assert book.on_quote("TEST", quote, "1d", observed_at=p.entry_date - timedelta(seconds=5)) == []
    assert book.open_count() == 1


@pytest.mark.parametrize("short", [False, True])
def test_scanner_checks_forming_live_prices_before_pattern_or_model_work(monkeypatch, short):
    book, p = book_with_position(short)
    book.assume_session_open = False
    scanner = MarketScanner(symbols=['TEST'], market='us', paper_account=book,
                            volume_gate=False, kronos_gate_mode='off', kronos_exit_mode='off')
    scanner._scan_snapshot_symbols, scanner._scan_daily_dates = set(), set()
    monkeypatch.setattr('core.scanner.is_closed_session_bar', lambda *a, **kw: False)
    price = 104 if short else 96
    snapshot = _make_snapshot('TEST', '1d', candle(100, 120, 80, price, 1))
    snapshot.timestamp = p.entry_date + timedelta(minutes=5)
    count = book.bar_count('TEST', '1d')
    assert scanner._apply_bar('TEST', '1d', snapshot) == (False, None)
    assert book.closed == [p]
    assert book.bar_count('TEST', '1d') == count
    assert scanner._cooldown_tracker[(p.symbol, p.pattern)] == (count, True)


@pytest.mark.parametrize("short", [False, True])
def test_quote_respects_close_confirmed_pattern_orders_but_not_for_safety_stop(short):
    book, p = book_with_position(short)
    p.stop_loss = 101 if short else 99
    p.stop_loss_on_close = p.exit_fill_at_close = True
    now = p.entry_date + timedelta(minutes=5)
    price = 102 if short else 98
    assert book.on_quote('TEST', candle(price, price, price, price, 1), '1d', observed_at=now) == []
    price = 104 if short else 96
    assert book.on_quote('TEST', candle(price, price, price, price, 1), '1d', observed_at=now) == [p]
    assert p.exit_reason == 'stop_loss'
