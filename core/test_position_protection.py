"""Economic outcomes of paper protection, including adoption and execution order."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.backtester import _check_exit, _open_trade, trade_risk_dollars
from core.paper_trader import PaperAccount, _trade_from_dict, _trade_to_dict, exit_state
from core.position_protection import ensure_paper_protection
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from utils.trade_export import build_paper_account_export


def book_with_position(short: bool, fee: float = .001, slip: float = 0) -> tuple:
    book = PaperAccount(market="us", txn_cost_pct=fee, slippage_pct=slip)
    book.assume_session_open = True
    ok, reason = book.open_position(signal(short), candle(100, 100, 100, 100), OHLCVStore())
    assert ok, reason
    return book, book.positions["TEST"][0]


@pytest.mark.parametrize("short", [False, True])
def test_known_execution_costs_cannot_exhaust_the_budget_before_a_trade_opens(short):
    book = PaperAccount(market="us", txn_cost_pct=.001, slippage_pct=.02)
    book.assume_session_open = True
    ok, reason = book.open_position(signal(short), candle(100, 100, 100, 100), OHLCVStore())
    assert not ok and "exhaust the 3%" in reason
    assert book.open_count() == 0
    assert book.cash == book.initial_capital


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("fee,slip", [(0, 0), (.001, 0), (.0035, .002)])
def test_stop_limits_net_loss_including_both_fees_and_adverse_slippage(short, fee, slip):
    book, p = book_with_position(short, fee, slip)
    original_notional = p.entry_price * p.qty
    # Wide intrabar excursion, with no opening gap through the resting order.
    assert book.on_bar("TEST", candle(100, 115, 85, 100, 1), "1d") == [p]
    assert p.exit_reason == "stop_loss"
    assert p.pnl_usd == pytest.approx(-.03 * original_notional)
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    assert book.equity() == pytest.approx(book.cash)
    assert trade_risk_dollars(p) < .031 * original_notional


@pytest.mark.parametrize("short", [False, True])
def test_opening_gap_retains_real_fill_and_can_exceed_three_percent(short):
    book, p = book_with_position(short, slip=.002)
    bar = candle(110, 115, 108, 112, 1) if short else candle(90, 92, 85, 88, 1)
    assert book.on_bar("TEST", bar, "1d") == [p]
    assert p.exit_price == pytest.approx(bar.open * (1.002 if short else .998))
    assert p.pnl_pct < -3
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)


@pytest.mark.parametrize("short", [False, True])
def test_trail_uses_surviving_close_then_preserves_net_profit_after_restart(short, tmp_path):
    book, p = book_with_position(short, slip=.002)
    first = candle(100, 101, 88, 90, 1) if short else candle(100, 112, 99, 110, 1)
    # Today's range crosses the new trail, which only exists AFTER the close.
    assert book.on_bar("TEST", first, "1d") == []
    initial = p.paper_protection["initial_stop"]
    ratcheted = p.paper_protection["stop"]
    assert ratcheted < initial if short else ratcheted > initial
    path = tmp_path / "paper.json"
    book.save(path)
    book = PaperAccount.load(path, market="us")
    p = book.positions["TEST"][0]
    assert p.paper_protection["stop"] == ratcheted
    # Retest the prior close, then reverse through the trail during the bar.
    bar = candle(90, 99, 89, 97, 2) if short else candle(110, 111, 101, 103, 2)
    assert book.on_bar("TEST", bar, "1d") == [p]
    assert p.exit_reason == "trailing_stop"
    assert p.pnl_pct > 6
    best_fill = first.close * (1.002 if short else .998)
    best_net = ((p.entry_price - best_fill if short else best_fill - p.entry_price)
                - (p.entry_price + best_fill) * .001) / p.entry_price * 100
    assert p.pnl_pct == pytest.approx(best_net - 3)  # Best CLOSE, not that bar's extreme.
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)
    check = p.position_marks[-1]["exit_check"]
    assert check["orders_before_bar"]["paper_protective_stop"] == ratcheted
    exported = build_paper_account_export(book)["books"][0]["closed_trades"][0]
    assert exported["paper_protection"] == p.paper_protection


@pytest.mark.parametrize("short", [False, True])
def test_small_profit_tightens_protection_without_activation_threshold_or_loosening(short):
    book, p = book_with_position(short, fee=0)
    first = candle(100, 101, 98, 99, 1) if short else candle(100, 102, 99, 101, 1)
    assert book.on_bar("TEST", first, "1d") == []
    stop = p.paper_protection["stop"]
    assert stop == pytest.approx(102 if short else 98)
    second = candle(99, 101, 98, 100, 2) if short else candle(101, 102, 99, 100, 2)
    assert book.on_bar("TEST", second, "1d") == []
    assert p.paper_protection["stop"] == stop


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("model", ["legacy", "causal-v1"])
def test_existing_open_positions_adopt_protection_and_keep_original_rule_evidence(short, model):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0, execution_model=model)
    p.resolved_rules["engine_sha256"] = "a63bde2d2a4cc481f5c74ee40c355aec2a3946a5c369da9b0ab7c0edeff7753c"
    p.position_marks = [{"sim_bar": candle(0, 0, 0, 0).timestamp.isoformat(), "close": 100},
                        {"sim_bar": candle(0, 0, 0, 0, 99).timestamp.isoformat(),
                         "close": 1 if short else 1000}]
    original = deepcopy(p.resolved_rules)
    p = _trade_from_dict(_trade_to_dict(p))
    book = PaperAccount(market="us", txn_cost_pct=.001)
    p.entry_fee_paid = 10
    book.positions["TEST"] = [p]
    book.cash += (1 if short else -1) * 10000 - 10
    assert book.on_bar("TEST", candle(100, 110, 90, 100, 1), "1d") == [p]
    assert p.pnl_pct == pytest.approx(-3)
    assert p.resolved_rules == original
    assert p.paper_protection["initial_stop"] is None
    assert p.paper_protection["peak_net_pct"] == 0  # Future marks cannot seed a trail.
    assert book.cash == pytest.approx(book.initial_capital + p.pnl_usd)


@pytest.mark.parametrize("short", [False, True])
def test_adoption_uses_completed_history_but_never_backdates_a_fill(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0, execution_model="causal-v1")
    prior = candle(100, 111, 89, 90 if short else 110, 1)
    p.position_marks = [{"sim_bar": prior.timestamp.isoformat(), "close": prior.close}]
    book = PaperAccount(market="us", txn_cost_pct=0, slippage_pct=0)
    book.positions["TEST"] = [p]
    book.cash += (1 if short else -1) * 10000
    current = candle(96, 99, 95, 98, 2) if short else candle(104, 105, 101, 102, 2)
    assert book.on_bar("TEST", current, "1d") == [p]
    assert p.paper_protection["stop"] == (93 if short else 107)
    assert p.exit_price == current.open
    assert p.sim_exit_date == current.timestamp


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("model", ["legacy", "causal-v1"])
def test_safety_stop_is_intrabar_even_when_pattern_waits_for_close(short, model):
    s = signal(short)
    s.exit_fill_at_close = s.stop_loss_on_close = True
    p = _open_trade(s, candle(100, 100, 100, 100), 0, execution_model=model)
    ensure_paper_protection(p, 0, 0, p.entry_date, at_entry=True)
    price, reason = _check_exit(candle(100, 110, 90, 100, 1), p, 1)
    assert (price, reason) == (103 if short else 97, "stop_loss")


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("model", ["legacy", "causal-v1"])
def test_nearer_structural_stop_wins_and_open_target_precedes_intrabar_stop(short, model):
    s = signal(short)
    s.stop_loss = 101 if short else 99
    p = _open_trade(s, candle(100, 100, 100, 100), 0, execution_model=model)
    ensure_paper_protection(p, 0, 0, p.entry_date, at_entry=True)
    assert _check_exit(candle(100, 110, 90, 100, 1), p, 1) == (s.stop_loss, "stop_loss")
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0, execution_model=model)
    ensure_paper_protection(p, 0, 0, p.entry_date, at_entry=True)
    bar = candle(65, 110, 60, 100, 1) if short else candle(135, 140, 90, 100, 1)
    assert _check_exit(bar, p, 1) == (bar.open, "take_profit")


@pytest.mark.parametrize("short", [False, True])
def test_queued_market_exit_executes_at_open_before_later_protective_touch(short):
    book, p = book_with_position(short)
    assert book.request_exit(p, "kronos_exit")
    bar = candle(99, 110, 95, 100, 1) if short else candle(101, 105, 90, 100, 1)
    assert book.on_bar("TEST", bar, "1d") == [p]
    assert p.exit_reason == "kronos_exit"
    assert p.exit_price == bar.open
    assert exit_state(p)["pending_exit_reason"] == "kronos_exit"
