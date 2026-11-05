"""Saved exit evidence separates prior orders, new closes and net results."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.backtester import _open_trade
from core.paper_trader import PaperAccount, _trade_from_dict, _trade_to_dict, exit_state
from core.test_patternsv2_exits import candle, signal
from utils.trade_export import _closed_row, _daily_mark


@pytest.mark.parametrize('short', [False, True])
def test_exit_preview_includes_cap_without_mutating_activation(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0,
                    execution_model='causal-v1')
    p._best_pnl_pct = .10
    p.lowest_close_since_entry = 90
    p.highest_close_since_entry = 110
    before = _trade_to_dict(p)
    state = exit_state(p)
    assert state['hard_stop'] == pytest.approx(105 if short else 95)
    assert state['trailing_stop'] == pytest.approx(94.5 if short else 104.5)
    assert state['trailing_activated'] is True
    assert _trade_to_dict(p) == before


@pytest.mark.parametrize('short', [False, True])
def test_saved_evidence_uses_prior_orders_and_retains_raw_bar_and_costs(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0,
                    execution_model='causal-v1')
    account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.002)
    p.entry_fee_paid = p.entry_price * p.qty * account.txn_cost_pct
    account.positions[p.symbol] = [p]
    account.cash += (1 if short else -1) * p.entry_price * p.qty - p.entry_fee_paid
    activation = candle(100, 102, 88, 90, 1) if short else candle(100, 112, 98, 110, 1)
    assert account.on_bar('TEST', activation, '1d') == []
    first = p.position_marks[-1]
    assert first['exit_check']['orders_before_bar']['trailing_stop'] is None
    assert first['exit_state']['trailing_stop'] == pytest.approx(94.5 if short else 104.5)
    paper_stop = first['exit_state']['paper_protective_stop']
    assert paper_stop < 94.5 if short else paper_stop > 104.5
    p = _trade_from_dict(_trade_to_dict(p))
    account.positions[p.symbol] = [p]
    gap = candle(97, 100, 95, 96, 2) if short else candle(102, 105, 100, 101, 2)
    closed = account.on_bar('TEST', gap, '1d')
    assert closed == [p]
    mark = _daily_mark(p.position_marks[-1])
    check = mark['exit_check']
    assert check['orders_before_bar']['trailing_stop'] == pytest.approx(94.5 if short else 104.5)
    assert check['raw_fill'] == gap.open
    assert check['fill'] == pytest.approx(gap.open * (1.002 if short else .998))
    assert check['ohlc']['close'] == gap.close
    assert mark['close'] == pytest.approx(p.exit_price, abs=.0001)
    assert check['reason'] == 'trailing_stop'
    gross = ((p.entry_price - p.exit_price) if short else (p.exit_price - p.entry_price)) * p.qty
    assert check['fees'] == pytest.approx(gross - p.pnl_usd)
    assert check['net_r'] == pytest.approx(p.pnl_usd / 500)
    assert account.cash == pytest.approx(account.initial_capital + p.pnl_usd)
    # Exported nested evidence is detached from the ledger.
    check['ohlc']['close'] = 0
    assert p.position_marks[-1]['exit_check']['ohlc']['close'] == gap.close


def test_legacy_export_does_not_invent_missing_exit_checks():
    assert _daily_mark({'close': 101})['exit_check'] is None
    assert _closed_row({'r': 2})['net_r'] is None


def test_legacy_indicator_close_also_saves_raw_fill_and_costs():
    p = _open_trade(signal(), candle(100, 100, 100, 100), 0)
    account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.002)
    account.indicators_only = True
    account.positions[p.symbol] = [p]
    bar = candle(104, 106, 103, 105, 1)
    assert account.exit_indicator(p.symbol, p, bar) is p
    check = p.position_marks[-1]['exit_check']
    assert check['raw_fill'] == 105
    assert check['fill'] == pytest.approx(104.79)
    assert check['ohlc']['open'] == 104
    assert check['reason'] == 'trailing_stop'
    assert check['net_r'] == pytest.approx(p.pnl_usd / 500)


def test_paper_summary_uses_simulation_dates_without_rewriting_fill_times():
    p = _open_trade(signal(), candle(100, 100, 100, 100), 0)
    p.sim_entry_date = datetime(2025, 12, 1, tzinfo=timezone.utc)
    p.sim_exit_date = p.sim_entry_date + timedelta(days=9)
    p.entry_date = datetime(2026, 11, 5, tzinfo=timezone.utc)
    p.exit_date = p.entry_date + timedelta(seconds=90)
    account = PaperAccount(market='us')
    account.closed = [p]
    result = account.to_result()
    assert result._s['avg_hold_days'] == 9
    assert result.trades[0].days_held == 9
    assert p.entry_date.year == 2026
    assert (p.exit_date - p.entry_date).total_seconds() == 90


def test_ui_and_cli_exports_include_the_same_execution_state(monkeypatch):
    from core.paper_books import PaperBook
    from utils.trade_export import build_paper_account_export, build_paper_trade_export

    p = _open_trade(signal(), candle(100, 100, 100, 100), 0, execution_model='causal-v1')
    account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=0)
    account.positions[p.symbol] = [p]
    account.on_bar(p.symbol, candle(100, 131, 99, 110, 1), '1d')
    monkeypatch.setattr('core.paper_books.PaperAccount.load', lambda **kwargs: account)
    monkeypatch.setattr('core.paper_books.load_signal_log', lambda *args: [])
    monkeypatch.setattr('core.signal_log_store.load_signal_log', lambda *args: [])
    cli = build_paper_account_export(account)['books'][0]['closed_trades'][0]
    ui = build_paper_trade_export({'books': {'us': PaperBook('us').snapshot()}})['books'][0]['closed_trades'][0]
    assert ui == cli
    assert ui['exit_state']['hard_stop'] == 95
    assert ui['net_r'] == pytest.approx(5.954)
    assert ui['daily_marks'][-1]['exit_check']['ohlc']['close'] == 110
