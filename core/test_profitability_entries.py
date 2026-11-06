"""Gap fills need positive net reward and a still-useful saved forecast."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from core.kronos_confirm import KronosConfirmGate
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_short_execution_pipeline import bar, candidate
from data.ohlcv_store import OHLCVStore


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('slip', [0, .001])
def test_target_that_loses_after_friction_is_rejected_without_fees(short, slip):
    book = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=slip)
    book.assume_session_open = True
    s = candidate(short)
    target = s.take_profit
    opening = target + .01 if short else target - .01
    store = OHLCVStore()
    assert book.queue_position(s, bar(100), store)[0]
    outcome = book.fill_pending('TEST','1d',bar(opening,1),store)[0]
    assert not outcome[1]
    assert 'economics' in outcome[2] or 'crossed the target' in outcome[2]
    assert book.cash == book.initial_capital and not book.positions


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('opening,allowed', [(100,True), (105,False)])
def test_saved_forecast_rebased_at_open_without_new_model_call(short, opening, allowed):
    s = candidate(short)
    opening = 200-opening if short else opening
    book = PaperAccount(market='us',txn_cost_pct=.001,slippage_pct=0)
    book.assume_session_open = True
    scanner = MarketScanner(symbols=['TEST'],market='us',paper_account=book,
                            kronos_gate_mode='confirm',kronos_exit_mode='off')
    fetch = Mock(return_value={'TEST':[-.04]*3 if short else [.04]*3})
    scanner._kronos_gate._fetch_fn = fetch
    scanner._kronos_gate.queue(s,bar(100),None)
    s.kronos_evidence = scanner._kronos_gate.flush()[0][3]
    store=OHLCVStore()
    assert book.queue_position(s,bar(100),store)[0]
    outcome=book.fill_pending('TEST','1d',bar(opening,1),store,entry_check=scanner._check_kronos_entry)[0]
    assert outcome[1] is allowed
    assert fetch.call_count==1
    if allowed:
        assert book.open_for('TEST')[0].kronos_evidence['opening_check']['decision']=='confirm'
    else:
        assert 'execution price' in outcome[2] and book.cash==book.initial_capital
