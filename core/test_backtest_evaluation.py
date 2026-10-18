"""Holdout declarations and aggregation never include training returns."""
from types import SimpleNamespace

import pytest

from core.backtest_params import settings_from_values
from core.backtest_evaluation import stitch_test_folds


def settings(**changes):
    values = dict(mode='historical-stream', market='us', symbols=['TEST'],
                  start_date='2024-02-01', end_date='2024-02-29', end_policy='force-close',
                  evaluation=dict(purpose='holdout', training_end='2024-01-31',
                                  strategy_frozen_at='2026-10-17T00:00:00Z',
                                  selection_note='Frozen strategy selected without examining the test interval',
                                  final_holdout_start='2024-04-01'))
    values.update(changes)
    return settings_from_values(values)


def fold(s, pnl, name):
    return dict(inputs=SimpleNamespace(request=SimpleNamespace(preset=SimpleNamespace(settings=s))),
                result=SimpleNamespace(run_id=name, metrics=SimpleNamespace(
                    net_pnl=pnl, fees=20., open_position_count=0)),
                equity=[dict(date='2024-01-01', equity=999999.),
                        dict(date=s.window.end_date.isoformat(), equity=100000.+pnl)])


def test_stitch_excludes_warmup_and_offsets_nonoverlapping_test_equity():
    first=fold(settings(), 100., 'first')
    second=fold(settings(start_date='2024-03-01',end_date='2024-03-31'),-50.,'second')
    result=stitch_test_folds([second,first])
    assert result['net_pnl'] == 50
    assert result['fees'] == 40
    assert result['equity'] == [dict(date='2024-02-29',equity=100100.),
                                dict(date='2024-03-31',equity=100050.)]
    assert result['run_ids'] == ['first','second']


def test_overlap_and_mixed_final_holdout_are_rejected():
    first=fold(settings(),100.,'first')
    with pytest.raises(ValueError,match='overlap'):
        stitch_test_folds([first,first])
    final=fold(settings(start_date='2024-04-01',end_date='2024-04-30', evaluation=dict(
        purpose='final-holdout', training_end='2024-03-31',strategy_frozen_at='2026-10-17T00:00:00Z',
        selection_note='Reserved final interval',final_holdout_start='2024-04-01')), 25.,'final')
    with pytest.raises(ValueError,match='separately'):
        stitch_test_folds([first, final])
    assert stitch_test_folds([final])['net_pnl'] == 25


def test_training_overlap_reserved_interval_and_open_boundaries_are_rejected():
    with pytest.raises(ValueError,match='Training outcomes'):
        settings(start_date='2024-01-31')
    with pytest.raises(ValueError,match='reserved final'):
        settings(end_date='2024-04-01')
    with pytest.raises(ValueError,match='force-close'):
        settings(end_policy='keep-open')
    broken=fold(settings(), 100.,'broken')
    broken['result'].metrics.open_position_count=1
    with pytest.raises(ValueError,match='boundary'):
        stitch_test_folds([broken])


def test_no_holdout_claim_without_freeze_evidence():
    with pytest.raises(ValueError, match='freeze time'):
        settings(evaluation=dict(purpose='holdout',training_end='2024-01-01'))
    research=fold(settings(evaluation=None),0,'research')
    with pytest.raises(ValueError,match='declared holdout'):
        stitch_test_folds([research])
