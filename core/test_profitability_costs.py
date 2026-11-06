"""Fresh books obey configured friction; old ledgers retain saved economics."""
from __future__ import annotations

import json
import pytest
from config import settings
from core.paper_trader import PaperAccount
from core.test_short_execution_pipeline import candidate, bar
from data.ohlcv_store import OHLCVStore


@pytest.mark.parametrize('short',[False,True])
def test_configured_slippage_charged_once_on_entry_and_exit(short,monkeypatch):
    monkeypatch.setattr(settings,'paper_slippage_pct',.002)
    book=PaperAccount(market='us',txn_cost_pct=.001)
    book.assume_session_open=True; store=OHLCVStore()
    assert book.queue_position(candidate(short),bar(100),store)[0]
    assert book.fill_pending('TEST','1d',bar(100,1),store)[0][1]
    p=book.open_for('TEST')[0]
    assert p.entry_price==pytest.approx(99.8 if short else 100.2)
    close=bar(110 if short else 90,2)
    assert book.on_bar('TEST',close,'1d')==[p]
    assert p.exit_price==pytest.approx(close.open*(1.002 if short else .998))
    assert book.cash==pytest.approx(book.initial_capital+p.pnl_usd)


def test_legacy_costs_never_inherit_a_new_process_default(tmp_path,monkeypatch):
    path=tmp_path/'old.json'
    path.write_text(json.dumps({'market':'us','initial_capital':100000}))
    monkeypatch.setattr(settings,'paper_slippage_pct',.01)
    assert PaperAccount.load(path,market='us').slippage_pct==0
    assert PaperAccount(market='us',slippage_pct=0).slippage_pct==0
