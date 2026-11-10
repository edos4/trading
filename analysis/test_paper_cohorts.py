"""Observed cohorts must not conflate missing DII evidence with a passing vote."""
from __future__ import annotations

from copy import deepcopy

import pytest

from analysis.paper_cohorts import cohort_review


def evidence():
    trades, rows, logs = [], [], []
    for i, (provenance, pattern, side, pnl, reason) in enumerate([
        ('patternsv2', 'falling_wedge', 'BUY', -30, 'stop_loss'),
        ('patternsv2', 'symmetrical_triangle', 'SELL', 20, 'take_profit'),
        ('legacy_unknown', 'old', 'BUY', 50, 'take_profit'),
        ('patternsv2', 'falling_wedge', 'BUY', -5, 'open'),
    ]):
        identity = str(i)
        trades.append({'trade_id': identity, 'signal_id': identity, 'provenance': provenance})
        rows.append({'trade_id': identity, 'pattern': pattern, 'action': side, 'pnl': pnl,
                     'exit_reason': reason, 'net_r': None, 'entry_date': '2026-01-05',
                     'marked_open_pnl': -4 if reason == 'open' else 0})
        if i < 2:
            logs.append({'trade_id': identity, 'signal_id': identity, 'run_id': 'run',
                         'status': 'filled', 'provenance': provenance, 'pattern': pattern, 'action': side})
    logs.append({**logs[0], 'status': 'shadow', 'reason': 'DII outside band'})
    logs.append({**logs[0], 'status': 'shadow', 'reason': 'DII duplicate stage'})
    return {'closed_trades': trades[:3], 'open_positions': trades[3:], 'signal_logs': logs}, rows


def test_family_side_dii_and_open_remainders_are_attributed_once():
    book, rows = evidence()
    original = deepcopy((book, rows))
    result = cohort_review(book, rows)
    assert result['included_trades'] == 3 and result['excluded_other_detectors'] == 1
    wedge = result['by_family_and_side']['falling_wedge|BUY']
    assert wedge['closed']['net_pnl'] == -30 and wedge['open_count'] == 1
    assert wedge['total_liquidation_pnl'] == -35 and wedge['open_marked_pnl'] == -4
    assert result['by_dii_shadow']['shadow_would_veto']['closed']['count'] == 1
    assert result['by_dii_shadow']['unknown_journal_linkage']['open_count'] == 1
    assert result['opportunities_by_family_and_side']['falling_wedge|BUY']['detected'] == 1
    assert result['opportunities_by_family_and_side']['falling_wedge|BUY']['dii_shadow_veto'] == 1
    assert result['automatic_filter_change'] is False
    assert (book, rows) == original


def test_ambiguous_replay_linkage_is_not_called_a_dii_pass():
    book, rows = evidence()
    book['signal_logs'].append({**book['signal_logs'][0], 'run_id': 'other-run'})
    result = cohort_review(book, rows)
    assert result['by_dii_shadow']['unknown_journal_linkage']['closed']['net_pnl'] == -30


def test_duplicate_or_missing_trade_rows_are_refused():
    book, rows = evidence()
    with pytest.raises(ValueError, match='unique trade'):
        cohort_review(book, rows+rows[:1])
    with pytest.raises(ValueError, match='same verified ledger'):
        cohort_review(book, rows[1:])


def test_exact_trade_identity_precedes_signal_reused_in_another_run():
    book, rows = evidence()
    book['signal_logs'].append({**book['signal_logs'][0], 'trade_id': 'another-trade', 'run_id': 'other-run'})
    result = cohort_review(book, rows)
    assert result['by_dii_shadow']['shadow_would_veto']['closed']['count'] == 1
