"""Direction training stays causal, reproducible, and separate from final tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
import json

import numpy as np
import pandas as pd
import pytest

from core.kronos_confirm import atr_pct_from_frame, sideways_band_pct
from core.kronos_direction import (
    DirectionConfig, DirectionModel, classification_metrics, direction_acceptance, evaluate_final,
    fit_direction, predict_direction, run_direction_experiment,
)
from learn.kronos_direction_dataset import DirectionCorpus, QualityPolicy, context_features, quality_masks
from learn.kronos_weekly_dataset import build_from_frames


def frame(phase: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range('2018-01-01', '2026-11-13')
    movement = np.roll(np.resize([.012, .012, -.012, -.012, 0., 0., .0001, -.0001], len(dates)), phase)
    close = 100 * np.exp(np.cumsum(movement))
    opening = np.concatenate(([close[0]], close[:-1]))
    return pd.DataFrame(dict(open=opening, high=np.maximum(opening, close) * 1.002,
                             low=np.minimum(opening, close) * .998, close=close, volume=1_000_000.), index=dates)


def corpus(path, *, end=date(2026, 11, 14), changed_history=False, lookback=64):
    sources = [('us', 'AAA', frame()), ('us', 'BBB', frame(1)),
               ('ph', 'BDO.PS', frame(2)), ('ph', 'CDE.PS', frame(3))]
    if changed_history:
        sources[0][2].iloc[0, :4] *= 1.01
    build_from_frames(sources, path, end=end)
    return DirectionCorpus(path, lookback=lookback)


def small_config(**overrides):
    options = dict(folds=2, validation_weeks=4, test_weeks=2, train_samples=256,
                   validation_samples=128, test_samples=128, epochs=4, batch_size=64,
                   min_eval_samples=10, min_eval_symbols=1, min_class_samples=1)
    options.update(overrides)
    return DirectionConfig(**options)


def test_features_and_flat_band_match_past_context_only():
    history = frame().iloc[:400]
    values = history.to_numpy()
    features, band = context_features(values, 'us')
    assert band == pytest.approx(sideways_band_pct(atr_pct_from_frame(history, history.close.iloc[-1])) / 100)
    future = np.concatenate((values, frame().iloc[400:410].to_numpy() * 10))
    np.testing.assert_array_equal(context_features(future[:400], 'us')[0], features)
    masks = quality_masks(future, 64, 'us', QualityPolicy())
    before = quality_masks(values, 64, 'us', QualityPolicy())
    for reason in before:
        np.testing.assert_array_equal(masks[reason][:len(before[reason])], before[reason])


@pytest.mark.parametrize('reason', ['stale_prices', 'zero_volume', 'low_turnover', 'price_discontinuity'])
def test_quality_rules_reject_past_contexts_without_changing_source(reason):
    values = frame().iloc[:400].to_numpy(copy=True)
    if reason == 'stale_prices':
        values[-60:, :4] = [100, 101, 99, 100]
    elif reason == 'zero_volume':
        values[-60:, 4] = 0
    elif reason == 'low_turnover':
        values[-60:, 4] = 1
    else:
        values[-30:, :4] *= 10
    original = values.copy()
    masks = quality_masks(values, 400, 'us', QualityPolicy())
    assert bool(masks[reason][0])
    np.testing.assert_array_equal(values, original)


def test_pools_keep_all_history_and_recency_draws_are_reproducible(tmp_path):
    data = corpus(tmp_path)
    uniform = data.pool(date.min, date(2026, 11, 7))
    recent = data.pool(date.min, date(2026, 11, 7), half_life_days=90, uniform_share=.1)
    a = uniform.sample(4000, 17, training=True)
    b = recent.sample(4000, 17, training=True)
    assert b == recent.sample(4000, 17, training=True)
    assert np.mean([data.days[t] for t, _ in b]) > np.mean([data.days[t] for t, _ in a]) + 600
    assert sum(data.days[t] < date(2021, 1, 1).toordinal() for t, _ in b) > 25
    assert sum(data.symbols[i]['market'] == 'ph' for _, i in b) == 2000
    validation = recent.sample(400, 19, training=False)
    assert len(validation) == len(set(validation))


def test_split_targets_and_embargo_do_not_overlap(tmp_path):
    data = corpus(tmp_path)
    boundary = date(2026, 8, 1)
    train = data.pool(date.min, boundary, gap_sessions=3).sample(1000, 42, training=True)
    validation = data.pool(boundary, date(2026, 10, 1), gap_sessions=3).sample(128, 43, training=False)
    for target, index in train:
        s = data.symbols[index]
        days = data.days[s['offset']:s['offset'] + s['count']]
        stop = np.searchsorted(days, boundary.toordinal()) + s['offset']
        assert target + 2 < stop - 3
        assert data.days[target + 2] < boundary.toordinal()
    assert min(data.days[t] for t, _ in validation) >= boundary.toordinal()
    assert max(data.days[t + 2] for t, _ in validation) < date(2026, 10, 1).toordinal()


def test_three_class_metrics_do_not_reward_flat_with_balanced_accuracy():
    actual = np.array([0, 1] + [2] * 8)
    metrics = classification_metrics(actual, np.full(10, 2))
    assert metrics['accuracy'] == .8
    assert metrics['balanced_accuracy'] == pytest.approx(1 / 3)
    assert metrics['classes']['up']['recall'] == metrics['classes']['down']['recall'] == 0


def separable_examples(seed, n, first_day):
    rng = np.random.default_rng(seed)
    labels = np.arange(n) % 3
    features = rng.normal(0, .01, (n, 20))
    features[np.arange(n), labels] += 1
    return dict(x=features, y=np.column_stack((labels, labels)), lookback=64,
                dates=np.column_stack((np.full(n, first_day), np.full(n, first_day + 2))),
                markets=np.asarray(['us', 'ph'] * (n // 2)))


def test_direct_classifier_learns_labels_and_selects_using_validation(tmp_path):
    train, validation = separable_examples(0, 600, 1000), separable_examples(1, 120, 1100)
    model, result = fit_direction(train, validation, 1, small_config(epochs=12, learning_rate=.05))
    assert (model.predict(validation['x']) == validation['y'][:, 0]).mean() > .95
    np.testing.assert_allclose(model.mean, train['x'].mean(axis=0))
    assert tuple(result['selection_score']) == max(tuple(row['selection_score']) for row in result['history'])
    model.save(tmp_path / 'model.npz')
    restored = DirectionModel.load(tmp_path / 'model.npz')
    assert restored.quality == model.quality
    np.testing.assert_array_equal(restored.probabilities(validation['x']), model.probabilities(validation['x']))
    with pytest.raises(ValueError, match='overlap'):
        fit_direction(train, {**validation, 'dates': train['dates'][:120]}, 1, small_config())


def test_final_period_stays_closed_and_later_append_is_verified(tmp_path):
    original = corpus(tmp_path / 'original', end=date(2026, 10, 3))
    output = tmp_path / 'experiment'
    selection = run_direction_experiment(original, output, date(2026, 10, 3), small_config())
    locked_selection = (output / 'selection.json').read_bytes()
    assert selection['selected_using'] == 'validation_only'
    assert not (output / 'final-evaluation.json').exists()
    assert selection['training']['1']['training_examples']['last_target'] < '2026-10-03'
    with pytest.raises(ValueError, match='not yet available'):
        evaluate_final(original, output, today=date(2026, 11, 15))
    extended = corpus(tmp_path / 'extended')
    report = evaluate_final(extended, output, today=date(2026, 11, 15))
    assert report['period'] == ['2026-10-03', '2026-10-31']
    assert report['examples']['first_target'] >= '2026-10-03'
    assert report['examples']['last_target'] < '2026-10-31'
    assert isinstance(report['passed_gates'], bool)
    assert (output / 'selection.json').read_bytes() == locked_selection
    with pytest.raises(ValueError, match='already consumed'):
        evaluate_final(extended, output, today=date(2026, 11, 15))


@pytest.mark.parametrize('tamper', ['checkpoint', 'selection', 'history', 'quality'])
def test_final_evaluation_rejects_changed_inputs(tmp_path, tamper):
    original = corpus(tmp_path / 'original', end=date(2026, 10, 3))
    output = tmp_path / 'experiment'
    run_direction_experiment(original, output, date(2026, 10, 3), small_config(epochs=1, folds=1))
    extended = corpus(tmp_path / 'extended', changed_history=tamper == 'history')
    if tamper == 'checkpoint':
        with (output / 'direction-1d.npz').open('ab') as stream:
            stream.write(b'tampered')
    elif tamper == 'selection':
        with (output / 'selection.json').open('a') as stream:
            stream.write(' ')
    elif tamper == 'quality':
        extended.quality = replace(extended.quality, us_min_turnover=1.)
    with pytest.raises(ValueError, match='differs|changed'):
        evaluate_final(extended, output, today=date(2026, 11, 15))
    assert not (output / 'final-evaluation.json').exists()


def test_dated_inference_uses_quality_and_suspension_rules(tmp_path):
    train, validation = separable_examples(0, 600, 1000), separable_examples(1, 120, 1100)
    model, _ = fit_direction(train, validation, 1, small_config(epochs=1))
    history = frame().iloc[:64]
    prediction = predict_direction(model, history, 'us')
    assert not prediction['abstained']
    assert sum(prediction['probabilities'].values()) == pytest.approx(1.)
    bad = history.copy()
    bad.iloc[-60:, 4] = 0
    assert 'zero_volume' in predict_direction(model, bad, 'us')['reasons']
    gapped = history.copy()
    gapped.index = list(history.index[:32]) + list(history.index[32:] + pd.Timedelta(days=40))
    assert 'long_gap' in predict_direction(model, gapped, 'us')['reasons']
    with pytest.raises(ValueError, match='policy differs'):
        predict_direction(model, history, 'us', replace(QualityPolicy(), us_min_turnover=1))


def test_cli_dry_run_does_not_read_corpus_or_write_output(tmp_path, capsys):
    from scripts.kronos_direction import main
    assert main(['--dataset', str(tmp_path / 'missing'), '--output', str(tmp_path / 'out'),
                 '--final-test-start', '2026-11-07', '--dry-run']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['excluded_symbols'] == ['AXRX', 'GASE', 'GBDMF']
    assert plan['quality']['us_min_turnover'] == 20_000_000
    assert not (tmp_path / 'out').exists()


def test_saved_kronos_comparison_rescores_identical_eligible_contexts(tmp_path):
    from core.kronos_direction_eval import compare_saved_kronos
    data = corpus(tmp_path / 'source')
    targets = data.pool(date(2026, 9, 1), date(2026, 10, 1)).sample(32, 42, training=False)
    saved = {'base': {'rows': []}, 'candidate': {'rows': []}}
    for target, index in targets:
        symbol = data.symbols[index]
        for name, forecast in [('base', .00001), ('candidate', .1)]:
            saved[name]['rows'].append(dict(market=symbol['market'], symbol=symbol['name'], horizon=1,
                                            valid=True, forecast_return=forecast,
                                            label_anchor_date=date.fromordinal(int(data.days[target - 1])).isoformat(),
                                            label_anchor_close=float(data.features[target - 1, 3])))
    path = tmp_path / 'saved.json'
    path.write_text(json.dumps(saved))
    weights = np.zeros((21, 3));weights[-1, 2] = 5
    model = DirectionModel(np.zeros(20), np.ones(20), weights, 1, 1, 64)
    report = compare_saved_kronos(data, model, path, date(2026, 9, 1), date(2026, 10, 1))
    assert report['matched_cases'] == 32
    assert report['test_role'] == 'reused_development_comparison'
    for group in report['groups'].values():
        assert group['direction_classifier'] == group['kronos_base']
        assert group['kronos_candidate']['classes']['up']['predicted'] == group['kronos_candidate']['samples']


def test_quality_is_applied_at_the_anchor_not_from_later_bars(tmp_path):
    data = corpus(tmp_path / 'source')
    targets = data.pool(date(2026, 9, 1), date(2026, 10, 1)).sample(24, 42, training=False)
    a = data.examples(targets)
    for row, (target, index) in enumerate(targets):
        context = np.array(data.features[target - data.lookback:target])
        expected, band = context_features(context, data.symbols[index]['market'])
        np.testing.assert_array_equal(a['x'][row], expected)
        assert a['bands'][row] == band
        for column, horizon in enumerate((1, 3)):
            actual = float(data.features[target + horizon - 1, 3]) / float(context[-1, 3]) - 1
            from core.kronos_confirm import classify
            from learn.kronos_direction_dataset import LABELS
            assert a['y'][row, column] == LABELS.index(classify(actual, band * 100))


def test_development_test_results_cannot_choose_sampling_or_weights(tmp_path, monkeypatch):
    import core.kronos_direction as direction
    data = corpus(tmp_path / 'source', end=date(2026, 10, 3))
    config = small_config(folds=1, epochs=1)
    a = run_direction_experiment(data, tmp_path / 'first', date(2026, 10, 3), config)
    monkeypatch.setattr(direction, 'evaluate_direction', lambda *_: {'irrelevant_test_score': 1e99})
    b = run_direction_experiment(data, tmp_path / 'second', date(2026, 10, 3), config)
    assert a['chosen_sampling'] == b['chosen_sampling']
    for horizon in (1, 3):
        first = DirectionModel.load(tmp_path / 'first' / f'direction-{horizon}d.npz')
        second = DirectionModel.load(tmp_path / 'second' / f'direction-{horizon}d.npz')
        np.testing.assert_array_equal(first.weights, second.weights)


def test_direction_index_enforces_the_requested_exclusions(tmp_path):
    sources = [('us', 'AAA', frame()), ('ph', 'BDO.PS', frame(1)),
               ('us', 'GBDMF', frame(2)), ('us', 'AXRX', frame(3)), ('us', 'GASE', frame())]
    build_from_frames(sources, tmp_path, end=date(2026, 11, 14))
    data = DirectionCorpus(tmp_path, lookback=64)
    assert {symbol['name'] for symbol in data.symbols} == {'AAA', 'BDO.PS'}
    draws = data.pool(date.min, date(2026, 11, 1)).sample(100, 42, training=True)
    assert {data.symbols[index]['name'] for _, index in draws} == {'AAA', 'BDO.PS'}


def test_high_raw_accuracy_from_always_flat_fails_research_gates():
    actual = np.array([0, 1] + [2] * 98)
    metric = classification_metrics(actual, np.full(100, 2))
    report = {name: {market: metric for market in ('us', 'ph')}
              for name in ('model', 'always_flat', 'persistence')}
    report['coverage'] = {market: {'symbols': 20} for market in ('us', 'ph')}
    failures = direction_acceptance({'1': report, '3': report}, small_config())
    assert metric['accuracy'] == .98
    assert any('baseline balanced_accuracy' in reason for reason in failures)
    assert any('baseline macro_f1' in reason for reason in failures)


@pytest.mark.parametrize('kwargs', [dict(gap_sessions=2), dict(half_life_days=0),
                                  dict(uniform_share=0), dict(selection_metric='accuracy')])
def test_invalid_direction_protocol_is_rejected(kwargs):
    with pytest.raises(ValueError):
        small_config(**kwargs)
