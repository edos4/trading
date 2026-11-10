"""V2 breakout admission uses a frozen, causal boundary in both directions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from core.execution import opening_candidate, validate_entry_geometry
from core.paper_trader import PaperAccount
from core.test_patternsv2_exits import candle, signal
from data.ohlcv_store import OHLCVStore
from utils.trade_export import build_paper_account_export


def candidate(short: bool):
    result = signal(short)
    result.signal_bar_timestamp = candle(100, 100, 100, 100).timestamp
    result.pattern_evidence['breakout_level'] = 100
    return result


@pytest.mark.parametrize('short', [False, True])
def test_open_back_inside_boundary_is_rejected_before_costs(short, tmp_path):
    book = PaperAccount(market='us', txn_cost_pct=.001, slippage_pct=.0005)
    book.assume_session_open = True
    s, store = candidate(short), OHLCVStore()
    assert book.queue_position(s, candle(100, 100, 100, 100), store)[0]
    path = tmp_path / 'book.json'
    book.save(path)
    book = PaperAccount.load(path, market='us')
    book.assume_session_open = True
    opening = 101 if short else 99
    result = book.fill_pending('TEST', '1d', candle(opening, 150, 50, 110, 1), store)
    assert len(result) == 1 and result[0][1] is False
    assert 'returned inside the Patternsv2 breakout boundary' in result[0][2]
    assert book.cash == book.initial_capital and not book.open_count()
    assert not book.pending_entries


@pytest.mark.parametrize('short', [False, True])
def test_slippage_cannot_manufacture_breakout_and_later_bar_range_is_irrelevant(short):
    s = candidate(short)
    raw = 100.01 if short else 99.99
    with pytest.raises(ValueError, match='breakout boundary'):
        opening_candidate(s, candle(raw, 150, 50, 101, 1), 10000, 'us', .001)
    raw = 99 if short else 101
    original = deepcopy(s)
    fill = opening_candidate(s, candle(raw, 150, 50, 80, 1), 10000, 'us', .001)
    assert fill.price == pytest.approx(raw * (.999 if short else 1.001))
    assert s == original
    assert fill.pattern_evidence == original.pattern_evidence


def test_old_queue_can_use_unique_saved_level_but_missing_or_conflicting_evidence_fails():
    s = candidate(False)
    s.pattern_evidence = None
    s.chart_annotations = [{'type': 'hline', 'label': 'level', 'price': 100}]
    validate_entry_geometry(s, 101)
    for annotations in ([], s.chart_annotations + [{'type': 'hline', 'label': 'level', 'price': 99}]):
        s.chart_annotations = annotations
        with pytest.raises(ValueError, match='boundary evidence'):
            validate_entry_geometry(s, 101)
    s.pattern_evidence = {'entry_boundary_policy': 'future-policy', 'breakout_level': 100}
    with pytest.raises(ValueError, match='Unknown Patternsv2'):
        validate_entry_geometry(s, 101)
    s.pattern_evidence = {'breakout_level': float('nan')}
    with pytest.raises(ValueError, match='boundary evidence'):
        validate_entry_geometry(s, 101)


def test_legacy_entries_do_not_acquire_v2_boundary_rules():
    s = replace(candidate(False), provenance='legacy_unknown', pattern_evidence=None)
    validate_entry_geometry(s, 99)


def test_boundary_evidence_survives_entry_and_export_without_aliasing():
    book = PaperAccount(market='us', txn_cost_pct=0, slippage_pct=0)
    book.assume_session_open = True
    s = candidate(False)
    assert book.open_position(s, candle(101, 101, 101, 101), OHLCVStore())[0]
    s.pattern_evidence['breakout_level'] = 999
    exported = build_paper_account_export(book)['books'][0]['open_positions'][0]
    assert exported['pattern_evidence']['breakout_level'] == 100
    assert exported['resolved_rules']['pattern_evidence']['breakout_level'] == 100
