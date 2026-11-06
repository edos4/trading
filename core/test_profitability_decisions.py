"""A transient detection failure cannot consume an already managed session."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock
import sys

import numpy as np
import pytest

from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle
from data.tv_client import MarketSnapshot
from patterns_v2.signals import Patternsv2Scan, detect_raw
from patterns_v2.loader import detect_fn


def test_failed_decision_retries_after_restart_without_replaying_ledger(monkeypatch,tmp_path):
    class Feed:
        advances=0
        async def fetch_snapshot(self,symbol,timeframe,store=None,**kwargs):
            bars=[candle(100,101,99,100,i) for i in range(60)]
            store.replace_all(symbol,timeframe,bars)
            return MarketSnapshot(symbol,timeframe,bars[-1].timestamp,bars[-1],{},{},{},{})
        async def advance_replay(self,session):
            self.advances+=1
            return True,False
    feed=Feed(); book=PaperAccount(market='us',slippage_pct=0)
    def scanner():
        s=MarketScanner(symbols=['TEST'],market='us',paper_account=book,data_feed=feed,
                        patterns_v2=True,kronos_gate_mode='off',kronos_exit_mode='off')
        s._patterns=[Patternsv2Scan()]
        return s
    s=scanner()
    detector=Mock(side_effect=RuntimeError('transient numerical error'))
    monkeypatch.setattr('patterns_v2.signals.detect_raw',detector)
    asyncio.run(s._scan_all(feed_sessions=[object()]))
    assert detector.call_count==1 and feed.advances==0
    assert book.bar_count('TEST','1d')==1
    assert book.processed_bar_identities_snapshot() and not book.decision_bar_identities_snapshot()
    path=tmp_path/'paper.json'; book.save(path)
    book=PaperAccount.load(path,market='us'); s=scanner()
    detector.side_effect=None; detector.return_value=[]
    asyncio.run(s._scan_all(feed_sessions=[object()]))
    assert detector.call_count==2 and feed.advances==1
    assert book.bar_count('TEST','1d')==1 and book.decision_bar_identities_snapshot()
    asyncio.run(s._scan_all(feed_sessions=[object()]))
    assert detector.call_count==2 and book.bar_count('TEST','1d')==1


def test_family_exception_is_incomplete_in_strict_scan_mode(monkeypatch):
    fn=detect_fn(); module=sys.modules[fn.__module__]
    def broken(*a,**kw):
        raise ArithmeticError('injected family failure')
    monkeypatch.setattr(module,'_BULLISH_DETECTORS',(broken,))
    c=np.full(60,100.)
    with pytest.raises(RuntimeError,match='Incomplete Patternsv2'):
        detect_raw(c+1,c-1,c,latest_bar_only=True)
