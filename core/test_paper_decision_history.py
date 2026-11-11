"""Exports retain complete, immutable model decisions and resolved controls."""
from __future__ import annotations

from types import SimpleNamespace

from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle
from core.run_manifest import record_observation
from utils.trade_export import _book_export, build_paper_account_export


def test_all_exit_decisions_survive_restart_export_and_later_mutation(tmp_path):
    book, position = book_with_position(False)
    scanner = MarketScanner(symbols=['TEST'], market='us', patterns_v2=True,
                            paper_account=book, kronos_gate_mode='off', kronos_exit_mode='exit')
    for decision, close in [('confirm', 100), ('oppose', 100.1), ('oppose', 106)]:
        evidence = {'schema_version': 1, 'anchor_date': '2026-01-05', 'decision': decision,
                    'anchor_close': close, 'atr_pct': 4, 'returns': [1, 2, 3]}
        scanner._handle_kronos_exit(position, SimpleNamespace(decision=decision, reason='test'), evidence)
        evidence['returns'].append(999)
    assert len(position.kronos_exit_history) == 3
    assert [row['evidence'].get('exit_action') for row in position.kronos_exit_history] == [
        None, 'queued_next_open', 'exit_already_pending']
    assert position.kronos_exit_history[0]['evidence']['returns'] == [1, 2, 3]
    path = tmp_path/'paper.json'
    book.save(path)
    restored = PaperAccount.load(path, market='us')
    row = build_paper_account_export(restored)['books'][0]['open_positions'][0]
    assert row['kronos_exit_history'] == position.kronos_exit_history
    row['kronos_exit_history'][0]['evidence']['returns'].append(999)
    assert restored.open_for('TEST')[0].kronos_exit_history[0]['evidence']['returns'] == [1, 2, 3]


def test_cli_and_web_exports_keep_source_controls_and_universe():
    book = PaperAccount(market='us')
    MarketScanner(symbols=['B', 'A'], market='us', patterns_v2=True, paper_account=book,
                  kronos_gate_mode='off', kronos_exit_mode='off')
    cli = build_paper_account_export(book)['books'][0]['execution_context']['run_manifest']
    web = _book_export({'execution_context': {'run_manifest': book.run_manifest}})['execution_context']['run_manifest']
    assert cli == web == book.run_manifest
    assert cli['entry_universe']['symbols'] == ['A', 'B']
    assert cli['execution']['patterns_v2'] is True
    assert cli['active_controls']['kronos_decision_policy'] == 'direction_only'
    assert 'kronos_exit_min_adverse_atr' not in cli['resolved_controls']
    assert 'patterns_v2/signals.py' in cli['source_sha256']
    assert cli['resolved_controls']['target_risk_basis'] == 'pattern'
    assert not any('password' in key or 'token' in key or 'dsn' in key for key in cli['resolved_controls'])


def test_tape_fingerprint_is_order_independent_between_symbols_but_detects_revisions():
    first, second = {}, {}
    a, b = candle(100, 101, 99, 100), candle(100, 102, 98, 101, 1)
    for symbol in ('A', 'B'):
        record_observation(first, symbol, '1d', a, [a])
        record_observation(first, symbol, '1d', b)
    for bar in (a, b):
        for symbol in ('B', 'A'):
            record_observation(second, symbol, '1d', bar, [a] if bar is a else None)
    assert first == second
    revised = {}
    record_observation(revised, 'A', '1d', a, [a])
    b.close = 100
    record_observation(revised, 'A', '1d', b)
    assert revised['observation_fingerprints']['A|1d']['sha256'] != first['observation_fingerprints']['A|1d']['sha256']
