"""Execution and review must agree on the selected target R denominator."""
from __future__ import annotations

import pytest

from analysis.paper_trade_review import replay_trade
from config import settings
from core.execution import apply_target_cap, opening_candidate
from core.paper_trader import PaperAccount
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from utils.trade_export import build_paper_account_export


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('basis', ['pattern', 'paper_initial'])
def test_queue_fill_export_and_target_diagnostic_agree(monkeypatch, short, basis):
    monkeypatch.setattr(settings, 'target_risk_basis', basis)
    monkeypatch.setattr(settings, 'target_max_r', 2)
    book = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.0005)
    book.assume_session_open = True
    store = OHLCVStore()
    assert book.queue_position(signal(short), candle(100, 100, 100, 100), store)[0]
    bar = candle(100, 101, 80, 85, 1) if short else candle(100, 120, 99, 115, 1)
    assert book.fill_pending('TEST', '1d', bar, store)[0][1]
    assert book.on_bar('TEST', bar, '1d')
    t = build_paper_account_export(book)['books'][0]['closed_trades'][0]
    geometry = t['execution_risk']
    assert geometry['target_risk_basis'] == basis
    assert geometry['pattern_distance'] > geometry['paper_initial_distance']
    assert geometry['paper_net_loss_budget_pct'] == .03
    sign = -1 if short else 1
    expected = round(t['resolved_rules']['price']+sign*2*geometry['target_risk_distance'], 4)
    assert t['target'] == expected
    replay = replay_trade(t, target_max_r=2)
    assert replay['pnl'] == pytest.approx(t['pnl'], abs=.00006)
    assert replay['target_cap_risk_basis'] == basis
    assert replay['target_cap_risk_distance'] == pytest.approx(geometry['target_risk_distance'])


def test_paper_basis_requires_explicit_cost_context(monkeypatch):
    monkeypatch.setattr(settings, 'target_risk_basis', 'paper_initial')
    monkeypatch.setattr(settings, 'target_max_r', 2)
    s = signal()
    s.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    with pytest.raises(ValueError, match='paper execution costs'):
        opening_candidate(s, candle(100, 110, 99, 108, 1), 10000, 'us')


@pytest.mark.parametrize('basis', ['pattern', 'paper_initial'])
def test_target_cap_preserves_a_nearer_measured_objective(basis):
    s = signal()
    s.take_profit = 101
    apply_target_cap(s, 100, 2, basis, paper_costs=(.001, .0005))
    assert s.take_profit == 101


def test_large_short_cap_does_not_reject_an_already_nearer_positive_target():
    s = signal(True)
    apply_target_cap(s, 100, 100, 'pattern')
    assert s.take_profit == 70


def test_optional_cap_does_not_invent_target_or_risk_when_both_are_absent():
    s = signal()
    s.take_profit = s.stop_loss = s.stop_loss_pct_cap = None
    geometry = apply_target_cap(s, 100, 2, 'pattern')
    assert s.take_profit is None and geometry['target_risk_distance'] is None
