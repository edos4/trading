"""Paper portfolios cannot spend short collateral or future exit proceeds."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from config import settings
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_short_execution_pipeline import candidate,bar
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns_v2.signals import Patternsv2Scan


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings,'max_open_positions',4)
    monkeypatch.setattr(settings,'max_open_per_pattern',0)
    monkeypatch.setattr(settings,'paper_max_open_per_symbol',1)
    monkeypatch.setattr(settings,'paper_max_gross_exposure',1.)


def book(capital=100000):
    b=PaperAccount(market='us',initial_capital=capital,txn_cost_pct=.001,
                   slippage_pct=0,account_model='funded')
    b.assume_session_open=True
    return b


def entry(b,symbol='TEST',short=False,day=0):
    return b.open_position(replace(candidate(short),symbol=symbol),bar(100,day),OHLCVStore())


def test_caps_cash_and_short_collateral_are_enforced(monkeypatch):
    b=book()
    assert entry(b)[0]
    assert not entry(b,short=True)[0]
    for name in ['B','C','D']:
        assert entry(b,name)[0]
    assert 'Portfolio full' in entry(b,'E')[1]
    b=book(15000)
    assert entry(b,short=True)[0]
    assert 'cash' in entry(b,'OTHER',short=True)[1]
    assert b.cash>15000  # proceeds cannot manufacture buying power


def test_pattern_and_daily_loss_limits(monkeypatch):
    monkeypatch.setattr(settings,'max_open_per_pattern',1)
    b=book(); assert entry(b)[0]
    assert 'Pattern position cap' in entry(b,'OTHER')[1]
    b.max_daily_loss=100
    assert b.on_bar('TEST',bar(90,1),'1d')
    assert 'Daily loss limit' in entry(b,'OTHER',day=1)[1]
    assert entry(b,'OTHER',day=2)[0]


def test_old_ledger_preserves_independent_semantics(tmp_path):
    p=tmp_path/'old.json'; p.write_text(json.dumps({'market':'us','initial_capital':100000}))
    assert PaperAccount.load(p,market='us').account_model=='independent-fixed-notional'
    b=book(); b.save(p)
    assert PaperAccount.load(p,market='us').account_model=='funded'


@pytest.mark.parametrize('order',[['AAA','BBB'],['BBB','AAA']])
def test_feed_arrival_cannot_choose_seat_or_spend_same_bar_future_profit(order,monkeypatch):
    monkeypatch.setattr(settings,'max_open_positions',0)
    b=book(15000); store=OHLCVStore()
    for name in ['AAA','BBB']:
        assert b.queue_position(replace(candidate(False),symbol=name),bar(100),store)[0]
    class Feed:
        snapshot_batch_size=1
        async def fetch_snapshot(self,symbol,timeframe,**kwargs):
            c=bar(100,1,high=140,low=100,close=130)
            return MarketSnapshot(symbol,timeframe,c.timestamp,c,{},{},{},{})
        async def advance_replay(self,session):
            return True,True
    s=MarketScanner(symbols=order,market='us',paper_account=b,data_feed=Feed(),patterns_v2=True,
                    kronos_gate_mode='off',kronos_exit_mode='off')
    s._patterns=[Patternsv2Scan()]
    asyncio.run(s._scan_all(feed_sessions=[object()]))
    assert [p.symbol for p in b.closed]==['AAA']
    assert [e['status'] for e in b.order_events]==['filled','rejected']
    assert b.order_events[1]['symbol']=='BBB' and 'cash' in b.order_events[1]['reason']
