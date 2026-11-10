"""A paired experiment cannot silently substitute partial or selected evidence."""
from __future__ import annotations

from copy import deepcopy

import pytest

from analysis.paper_policy_evaluation import compare_books, create_plan, model_holdout_evidence
from core.paper_trader import PaperAccount
from core.run_manifest import record_observation
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from utils.trade_export import build_paper_account_export


def experiment():
    arms = []
    for mode in ('off', 'confirm'):
        account = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=0)
        account.assume_session_open = True
        MarketScanner(symbols=['TEST'], market='us', patterns_v2=True, paper_account=account,
                      kronos_gate_mode=mode, kronos_exit_mode='off')
        s = signal()
        s.signal_id = 'decision'
        if mode == 'confirm':
            s.kronos_evidence = {'schema_version': 1, 'scope': 'entry', 'symbol': 'TEST',
                'action': 'BUY', 'anchor_date': '2026-01-05', 'model_identity': 'frozen-model',
                'model_provenance': {'identity_verified': True, 'out_of_sample_eligible': True,
                                     'selection_data_end': '2025-12-31', 'eligible_from': '2026-01-01'}}
        store = OHLCVStore()
        first, second = candle(100, 100, 100, 100), candle(100, 135, 99, 130, 1)
        assert account.queue_position(s, first, store)[0]
        assert account.fill_pending('TEST', '1d', second, store)[0][1]
        assert account.on_bar('TEST', second, '1d')
        manifest = account.run_manifest
        record_observation(manifest, 'TEST', '1d', first, [first])
        record_observation(manifest, 'TEST', '1d', second)
        trade = account.closed[0]
        logs = [{'status': 'filled', 'signal_id': s.signal_id, 'trade_id': trade.trade_id,
                 'symbol': 'TEST', 'action': 'BUY', 'provenance': 'patternsv2',
                 'ts': trade.exit_date.isoformat(),
                 'run_id': manifest['run_id'], 'kronos_evidence': s.kronos_evidence}]
        export = build_paper_account_export(account, scan_stats={
            'scan_complete': True, 'termination_reason': 'end_of_tape',
            'run_started_at': manifest['created_at'], 'run_finished_at': trade.exit_date.isoformat(),
            'run_totals': {'patterns_found': 1, 'trades_opened': 1, 'signals_rejected': 0}})
        export['books'][0]['signal_logs'] = logs
        arms.append(export['books'][0])
    plan = create_plan(*arms, start='2026-01-05', end='2026-01-06',
                       changes=['active_controls.kronos_entry'], model_identity='frozen-model',
                       registered_at='2026-01-04T00:00:00+00:00')
    return arms, plan


def test_complete_paired_runs_reproduce_and_compare_without_mutation():
    arms, plan = experiment()
    original = deepcopy(arms)
    comparison = compare_books(*arms, plan)
    assert comparison['eligible'], comparison['issues']
    assert comparison['liquidation_pnl_difference'] == 0
    assert comparison['runs'][1]['model_evidence']['forecasts_checked'] > 0
    assert arms == original


@pytest.mark.parametrize('corruption,message', [
    ('tape', 'price histories'), ('coverage', 'unresolved execution'),
    ('source', 'source differs'), ('drift', 'controls changed'),
    ('journal', 'journal'), ('funding', 'fixed controls'), ('model', 'model holdout'),
    ('cutoff', 'declared window'), ('history', 'decision history'),
])
def test_invalid_pair_cannot_claim_a_policy_profit_improvement(corruption, message):
    arms, plan = experiment()
    book = arms[1]
    manifest = book['execution_context']['run_manifest']
    if corruption == 'tape': manifest['observation_fingerprints']['TEST|1d']['sha256'] = 'changed'
    elif corruption == 'coverage': book['execution_context']['execution_coverage']['verified'] = False
    elif corruption == 'source': manifest['source_sha256'] = {}
    elif corruption == 'drift': manifest['control_changes'] = [{'changed': True}]
    elif corruption == 'journal': book['signal_logs'] = []
    elif corruption == 'funding': manifest['execution']['initial_capital'] *= 2
    elif corruption == 'model': book['closed_trades'][0]['kronos_evidence']['model_provenance']['out_of_sample_eligible'] = False
    elif corruption == 'cutoff': book['execution_context']['sim_as_of'] = '2026-01-07T00:00:00+00:00'
    elif corruption == 'history': del manifest['exit_journal_policy']
    result = compare_books(*arms, plan)
    assert not result['eligible'] and result['liquidation_pnl_difference'] is None
    assert any(message in issue for issue in result['issues']), result['issues']


def test_plan_cannot_be_registered_after_seeing_the_evaluation_period():
    arms, plan = experiment()
    plan['registered_at'] = '2026-01-06T00:00:00+00:00'
    result = compare_books(*arms, plan)
    assert not result['eligible'] and 'before the evaluation window' in result['issues'][0]


def test_identity_flag_alone_does_not_establish_a_holdout():
    arms, _ = experiment()
    book = arms[1]
    for row in [book['closed_trades'][0]['kronos_evidence'], book['signal_logs'][0]['kronos_evidence']]:
        row['model_provenance']['selection_data_end'] = '2026-01-05'
        row['model_provenance']['eligible_from'] = '2026-01-06'
    result = model_holdout_evidence(book, 'frozen-model')
    assert not result['eligible']
    assert any('anchor precedes' in reason for row in result['ineligible_forecasts'] for reason in row['reasons'])
