"""A replay budget defers decisions at the same date without losing trades."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")
from config import settings
from core.kronos_confirm import Forecast
from core.kronos_process import KronosBusy
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_short_execution_pipeline import bar,candidate
from data.ohlcv_store import OHLCVStore
from data.tv_client import MarketSnapshot
from patterns_v2.signals import Patternsv2Scan


def setup(monkeypatch,symbols):
    monkeypatch.setattr(settings,'max_open_per_pattern',0)
    class Feed:
        advances=0
        async def fetch_snapshot(self,symbol,timeframe,store=None,**kwargs):
            bars=[replace(bar(100,i),high=101,low=99,volume=1e6) for i in range(-59,1)]
            store.replace_all(symbol,timeframe,bars)
            return MarketSnapshot(symbol,timeframe,bars[-1].timestamp,bars[-1],{},{},{},{})
        async def advance_replay(self,session):
            self.advances+=1
            return True,False
    feed=Feed(); book=PaperAccount(market='us',slippage_pct=0,account_model='funded')
    book.assume_session_open=True
    scanner=MarketScanner(symbols=symbols,paper_account=book,data_feed=feed,market='us',patterns_v2=True,
                          kronos_gate_mode='confirm',kronos_exit_mode='exit',volume_gate=False)
    scanner._patterns=[Patternsv2Scan()]; scanner._running=True
    scanner._kronos_gate.max_checks_per_scan=1
    scanner._kronos_gate._fetch_fn=lambda symbols:{s:[.05,.06,.07] for s in symbols}
    scanner._append_signal_log=Mock()
    return scanner,book,feed


@pytest.mark.parametrize('exits',[False,True])
def test_budget_drains_across_same_session_without_repeat_management(monkeypatch,exits):
    scanner,book,feed=setup(monkeypatch,['CCC','AAA','BBB'])
    if exits:
        for symbol in ['CCC','AAA','BBB']:
            assert book.open_position(replace(candidate(False),symbol=symbol),bar(100),OHLCVStore())[0]
    async def analyze(snapshots):
        if not exits:
            for snap in snapshots:
                await scanner._emit_signals(snap,[replace(candidate(False),symbol=snap.symbol)])
    scanner._run_analyzes=analyze
    for attempt in range(3):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
        assert feed.advances==int(attempt==2)
    assert all(book.bar_count(symbol,'1d')==1 for symbol in ['AAA','BBB','CCC'])
    assert len(book.decision_bar_identities_snapshot())==3
    assert len(book.pending_entries)==(0 if exits else 3)
    assert scanner.stats['kronos_deferred']==3
    assert scanner.stats['scan_complete']


def test_busy_worker_stops_incomplete_after_bounded_retry(monkeypatch):
    scanner,book,feed=setup(monkeypatch,['AAA'])
    scanner._kronos_gate._fetch_fn=Mock(side_effect=KronosBusy('GPU busy'))
    async def analyze(snapshots):
        for snap in snapshots:
            await scanner._emit_signals(snap,[replace(candidate(False),symbol=snap.symbol)])
    scanner._run_analyzes=analyze
    for _ in range(3):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert feed.advances==0 and not scanner._running
    assert scanner.stats['termination_reason']=='kronos_unavailable'
    assert not book.pending_entries and not book.decision_bar_identities_snapshot()


def test_shadow_model_failure_does_not_defer_or_block_replay(monkeypatch):
    scanner,book,feed=setup(monkeypatch,['AAA'])
    scanner._kronos_mode=scanner._kronos_gate.mode='shadow'
    scanner._kronos_gate._fetch_fn=Mock(side_effect=KronosBusy('GPU busy'))
    async def analyze(snapshots):
        for snap in snapshots:
            await scanner._emit_signals(snap,[replace(candidate(False),symbol=snap.symbol)])
    scanner._run_analyzes=analyze
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert feed.advances==1 and len(book.pending_entries)==1
    assert scanner.stats.get('kronos_deferred',0)==0


def test_retry_limit_counts_context_observations_not_pattern_rows(monkeypatch):
    scanner,book,feed=setup(monkeypatch,['AAA'])
    scanner._kronos_gate._fetch_fn=Mock(side_effect=KronosBusy('GPU busy'))
    async def analyze(snapshots):
        for snap in snapshots:
            await scanner._emit_signals(snap,[replace(candidate(False),symbol=snap.symbol,pattern=pattern)
                for pattern in ['double_bottom','bull_flag','inv_head_shoulders']])
    scanner._run_analyzes=analyze
    for _ in range(2):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
        assert scanner._running
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert not scanner._running and scanner._kronos_gate._fetch_fn.call_count==3


def _short_history(symbols):
    """Worker rows for a symbol whose own series is shorter than MIN_BARS."""
    return {symbol: Forecast((),'',0,0,error='InsufficientHistory: only 49 bars; need 60',
                             error_kind='insufficient_history') for symbol in symbols}


def test_short_history_entry_is_recorded_without_stopping_the_replay(monkeypatch):
    """A listing too short to model must not end a replay.

    The refusal is deterministic for its anchor, so deferring it spent the
    bounded retry and the third try set termination_reason='kronos_unavailable':
    one short-history symbol stopped a 31-session replay at its own anchor.
    """
    scanner,book,feed=setup(monkeypatch,['AAA'])
    scanner._kronos_gate._fetch_fn=_short_history
    async def analyze(snapshots):
        for snap in snapshots:
            await scanner._emit_signals(snap,[replace(candidate(False),symbol=snap.symbol)])
    scanner._run_analyzes=analyze
    for _ in range(3):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
        assert scanner._running
    assert feed.advances==3
    assert scanner.stats.get('kronos_deferred',0)==0
    assert 'termination_reason' not in scanner.stats
    assert scanner.stats['kronos_symbol_history']==1
    assert not book.pending_entries
    # Recorded as a terminal rejection for the anchor, never as a deferral:
    # a deferral is what spends the bounded retry that stops the run.
    kronos_rows=[call.kwargs for call in scanner._append_signal_log.call_args_list
                 if str(call.kwargs.get('reason','')).startswith('Kronos')]
    assert kronos_rows and {row['status'] for row in kronos_rows}=={'rejected'}


def test_short_history_position_is_neither_deferred_nor_exited(monkeypatch):
    """A held short-history symbol keeps its other exits and blocks nothing."""
    scanner,book,feed=setup(monkeypatch,['AAA'])
    assert book.open_position(replace(candidate(False),symbol='AAA'),bar(100),OHLCVStore())[0]
    scanner._kronos_gate._fetch_fn=_short_history
    async def analyze(snapshots):
        return None
    scanner._run_analyzes=analyze
    for _ in range(3):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert feed.advances==3 and scanner._running
    assert scanner.stats.get('kronos_deferred',0)==0
    assert scanner.stats.get('kronos_exits',0)==0
    position=book.open_for('AAA')[0]
    assert not getattr(position,'pending_exit_reason','')
