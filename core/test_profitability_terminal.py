"""Cutoffs exclude later executions; open marks disclose exit friction."""
from __future__ import annotations

import asyncio
import pytest
from core.scanner import MarketScanner
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle
from data.tv_client import MarketSnapshot
from patterns_v2.signals import Patternsv2Scan


@pytest.mark.parametrize('short',[False,True])
def test_net_liquidation_stress_is_cost_aware_and_never_settles(short):
    book,p=book_with_position(short,fee=.001,slip=.002)
    cash=book.cash
    context=book.snapshot_metrics()['execution_context']
    estimate=context['hypothetical_liquidation']
    raw=book.last_price('TEST',100)
    expected=raw*p.qty*.002+raw*(1.002 if short else .998)*p.qty*.001
    assert estimate['additional_exit_costs']==pytest.approx(expected)
    assert estimate['equity_after_exit_costs']==pytest.approx(book.equity()-expected)
    assert book.cash==cash and book.open_count()==1
    assert not context['corporate_action_verification']['verified']


def test_session_after_cutoff_cannot_execute_or_mark(monkeypatch):
    book,p=book_with_position(False,slip=0)
    class Feed:
        advances=0
        async def fetch_snapshot(self,symbol,timeframe,**kwargs):
            c=candle(80,81,79,80,2)
            return MarketSnapshot(symbol,timeframe,c.timestamp,c,{},{},{},{})
        async def advance_replay(self,session):
            self.advances+=1; return True,False
    feed=Feed()
    s=MarketScanner(symbols=['TEST'],paper_account=book,data_feed=feed,patterns_v2=True,
                    market='us',kronos_gate_mode='off',kronos_exit_mode='off',stream_end='2026-01-06')
    s._patterns=[Patternsv2Scan()]; s._running=True
    cash=book.cash
    asyncio.run(s._scan_all(feed_sessions=[object()]))
    assert book.open_count()==1 and book.cash==cash and feed.advances==0
    assert not s._running and s.stats['termination_reason']=='stream_end_reached'
    assert book.last_price('TEST',0)==100
