"""Audit exports must retain decision evidence and execution health."""
from __future__ import annotations

from copy import deepcopy
import json

from core import signal_log_store as logs
from core.paper_trader import PaperAccount
from core.run_manifest import build_run_manifest
from main import _write_trades_log
from utils.trade_export import build_paper_account_export


def test_export_retains_full_journal_and_health(monkeypatch,tmp_path):
    monkeypatch.setattr(logs,'_log_dir',tmp_path)
    for i in range(2101):
        logs.append_signal_log('us',{'signal_id':str(i),'symbol':'TEST','status':'rejected',
                                    'decision_context':[{'close':100}],'run_id':'run'})
    assert len(logs.load_signal_log('us'))==1000  # bounded UI read
    assert len(logs.load_signal_log('us',limit=0))==2101
    health={'snapshot_errors':5,'asof_skipped':20,'dead_symbols':50,'symbols_total':2000,
            'symbols_with_snapshot':1600,'entry_symbols_total':1998,'execution_watchlist_pairs':2,
            'run_totals':{'signals_rejected':2101}}
    book=PaperAccount(market='us',slippage_pct=0)
    book.run_manifest=build_run_manifest(patterns_v2=True,market='us')
    export=build_paper_account_export(book,scan_stats=health)['books'][0]
    assert export['scan_stats']==health
    assert len(export['signal_logs'])==2101
    assert export['signal_logs'][0]['decision_context']==[{'close':100}]
    assert export['execution_context']['run_manifest'] == book.run_manifest
    export['execution_context']['run_manifest']['resolved_controls']['target_max_r'] = 999
    assert book.run_manifest['resolved_controls']['target_max_r'] != 999
    assert book.run_manifest['source_sha256']
    export['scan_stats']['snapshot_errors']=0
    assert health['snapshot_errors']==5


def test_cli_export_replaces_complete_file_atomically(tmp_path):
    book=PaperAccount(market='us',slippage_pct=0)
    path=tmp_path/'export.json'
    path.write_text('previous export')
    _write_trades_log(str(path),book,status='Stopped: end_of_tape')
    assert json.loads(path.read_text())['books'][0]['status']=='Stopped: end_of_tape'
