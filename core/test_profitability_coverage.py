"""Missing prices cannot certify a portfolio or silently drop held names."""
from __future__ import annotations

import pytest

from core.scanner import MarketScanner
from core.paper_trader import PaperAccount
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle
from data.stream_client import FetchSkip
from utils.trade_export import build_paper_account_export


@pytest.mark.parametrize('code',['no_data','asof_mismatch','history_unavailable','invalid_candle'])
def test_missing_held_name_retained_and_run_stops_incomplete_after_bounded_retry(code,tmp_path):
    book,p=book_with_position(False)
    s=MarketScanner(symbols=['TEST'],paper_account=book,market='us',patterns_v2=True,
                    kronos_gate_mode='off',kronos_exit_mode='off')
    s._running=True; s._replay_asof='2026-01-06'
    for _ in range(3):
        s._note_fetch_skip('TEST',FetchSkip(code,'no certified execution bar'))
    assert not s._running and 'TEST' not in s._dead_symbols
    assert book.open_count()==1 and s.stats['snapshot_errors']==3
    assert s.stats['termination_reason']=='missing_execution_bars'
    path=tmp_path/'paper.json'; book.save(path); book=PaperAccount.load(path,market='us')
    book.on_bar('TEST',candle(100,101,99,100,3),'1d')
    coverage=build_paper_account_export(book)['books'][0]['execution_context']['execution_coverage']
    assert not coverage['verified'] and len(coverage['unresolved_intervals'])==1
    assert coverage['last_observed_at']['TEST']==candle(0,0,0,0,3).timestamp.isoformat()
