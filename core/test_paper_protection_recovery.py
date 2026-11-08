"""Saved longs and shorts retain executable protection through feed/runtime faults."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from core.backtester import ExitRuntimeChangedError, _check_exit, _make_snapshot
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle
from core.test_position_protection import book_with_position


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("model", ["legacy", "causal-v1"])
@pytest.mark.parametrize("adopted", [False, True])
def test_incompatible_pattern_engine_cannot_block_net_three_percent_exit(short, model, adopted, tmp_path):
    book, p = book_with_position(short, slip=.002)
    p.execution_model = model
    if model == "legacy":
        p.pattern_version_id = "saved-pattern-version"
    if adopted:
        p.paper_protection = None
    p.resolved_rules["engine_sha256"] = "unavailable-pattern-runtime"
    rules = deepcopy(p.resolved_rules)
    path = tmp_path / "paper.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    p = book.positions["TEST"][0]
    bar = candle(100, 115, 85, 100, 1)
    with pytest.raises(ExitRuntimeChangedError):
        _check_exit(bar, p, 1)
    assert book.on_bar("TEST", bar, "1d") == [p]
    assert p.pnl_pct == pytest.approx(-3)
    assert p.resolved_rules == rules
    assert p.position_marks[-1]["exit_check"]["management_mode"] == "paper_protection_only"
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    assert book.on_bar("TEST", bar, "1d") == []


@pytest.mark.parametrize("short", [False, True])
def test_recovery_keeps_an_already_queued_market_exit_ahead_of_later_stop_touch(short):
    book, p = book_with_position(short)
    p.resolved_rules["engine_sha256"] = "unavailable-pattern-runtime"
    assert book.request_exit(p, "kronos_exit")
    bar = candle(99, 110, 95, 100, 1) if short else candle(101, 105, 90, 100, 1)
    assert book.on_bar("TEST", bar, "1d") == [p]
    assert p.exit_price == bar.open
    assert p.exit_reason == "kronos_exit"


@pytest.mark.parametrize("short", [False, True])
def test_recovery_ratchets_surviving_close_then_fills_real_opening_gap(short):
    book, p = book_with_position(short)
    p.resolved_rules["engine_sha256"] = "unavailable-pattern-runtime"
    first = candle(100, 101, 88, 90, 1) if short else candle(100, 112, 99, 110, 1)
    assert book.on_bar("TEST", first, "1d") == []
    stop = p.paper_protection["stop"]
    gap = candle(97, 99, 96, 98, 2) if short else candle(103, 105, 101, 102, 2)
    assert book.on_bar("TEST", gap, "1d") == [p]
    assert p.exit_price == gap.open
    assert p.exit_reason == "trailing_stop"
    assert stop < gap.open if short else stop > gap.open


@pytest.mark.parametrize("short", [False, True])
def test_delayed_candle_cannot_apply_a_new_stop_to_an_old_range(short):
    book, p = book_with_position(short, fee=0)
    future = candle(100, 101, 89, 90, 2) if short else candle(100, 111, 99, 110, 2)
    assert book.on_bar("TEST", future, "1d") == []
    policy, marks = deepcopy(p.paper_protection), deepcopy(p.position_marks)
    equity, count = book.equity(), book.bar_count("TEST", "1d")
    # This older range would cross the newly tightened trail, but not the
    # original stop. Duplicate completed bars are equally unable to recheck it.
    assert book.on_bar("TEST", candle(100, 101, 99, 100, 1), "1d") == []
    assert book.on_bar("TEST", future, "1d") == []
    assert p.paper_protection == policy
    assert p.position_marks == marks
    assert book.equity() == equity
    assert book.bar_count("TEST", "1d") == count


@pytest.mark.parametrize("short", [False, True])
def test_old_ledger_uses_saved_mark_clock_when_sim_entry_is_missing(short):
    book, p = book_with_position(short)
    # entry_date is today's actual fill time; the old ledger lost the
    # separate simulated entry field but still has its January entry mark.
    p.sim_entry_date = None
    assert book.on_bar("TEST", candle(100, 115, 85, 100, 1), "1d") == [p]
    assert p.pnl_pct == pytest.approx(-3)


@pytest.mark.parametrize("short", [False, True])
def test_live_runtime_recovery_uses_only_independent_stop(short):
    book, p = book_with_position(short)
    p.resolved_rules["engine_sha256"] = "unavailable-pattern-runtime"
    # The incompatible pattern target/stop cannot be reinterpreted.
    p.stop_loss = 100.1 if short else 99.9
    now = p.entry_date + timedelta(seconds=5)
    assert book.on_quote("TEST", candle(100, 102, 98, 101 if short else 99, 1), "1d", observed_at=now) == []
    price = 104 if short else 96
    assert book.on_quote("TEST", candle(price, price, price, price, 1), "1d", observed_at=now) == [p]
    assert p.exit_price == price
    assert p.position_marks[-1]["exit_check"]["management_mode"] == "paper_protection_only"


@pytest.mark.parametrize("short", [False, True])
def test_bad_forming_range_does_not_hide_a_valid_live_stop_observation(monkeypatch, short):
    book, p = book_with_position(short)
    book.assume_session_open = False
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book,
                            volume_gate=False, kronos_gate_mode="off", kronos_exit_mode="off")
    monkeypatch.setattr("core.scanner.is_closed_session_bar", lambda *args, **kwargs: False)
    price = 104 if short else 96
    snapshot = _make_snapshot("TEST", "1d", candle(100, 99, 101, price, 1))
    snapshot.timestamp = p.entry_date + timedelta(seconds=5)
    assert scanner._apply_bar("TEST", "1d", snapshot) == (False, None)
    assert book.closed == [p]
    assert p.exit_price == price
    assert p.position_marks[-1]["exit_check"]["observation_kind"] == "live_quote"


@pytest.mark.parametrize("short", [False, True])
def test_malformed_historical_range_cannot_be_reinterpreted_as_a_live_quote(short):
    book, p = book_with_position(short)
    scanner = MarketScanner(symbols=["TEST"], market="us", paper_account=book)
    price = 104 if short else 96
    snapshot = _make_snapshot("TEST", "1d", candle(100, 99, 101, price, 1))
    assert scanner._apply_bar("TEST", "1d", snapshot) == (False, None)
    assert book.open_for("TEST") == [p]
    assert not book.closed
    assert book.execution_gaps
