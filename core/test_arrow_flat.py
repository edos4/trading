"""Flat decisions are fitted on earlier labels and leave frozen Arrow versions usable."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json

import numpy as np
import pytest

from core.arrow import Arrow, register_arrow
from core.arrow_flat import GROUPS, apply_policy, build_revision, evaluate_revision, fit_policy, wrap_model
from core.kronos_direction import DirectionModel
from core.test_arrow import bundle
from core.test_kronos_direction import corpus, frame
from learn.kronos_direction_dataset import DirectionCorpus
from learn.kronos_weekly_dataset import file_hash


def policy(bias=.5):
    return {'method': 'us_1d_liquidity_flat_bias_v1', 'turnover_thresholds_usd': [250e6, 1e9],
            'biases': dict.fromkeys(GROUPS, bias)}


def examples():
    x = np.zeros((300, 20))
    x[:, 15] = np.repeat(np.log1p([100e6, 500e6, 2e9]), 100)
    y = np.tile([0] * 20 + [1] * 20 + [2] * 60, 3)
    probabilities = np.asarray([[.7, .2, .1], [.2, .7, .1], [.4, .35, .25]])[y]
    return probabilities, {'x': x, 'y': np.column_stack((y, y)), 'markets': np.full(300, 'us')}


def test_validation_fits_flat_without_collapsing_up_or_down():
    raw, data = examples()
    fitted, report = fit_policy(raw, data)
    assert fitted['biases'] == dict.fromkeys(GROUPS, .5)
    predicted = apply_policy(raw, data['x'], 1, fitted).argmax(axis=1)
    np.testing.assert_array_equal(predicted, data['y'][:, 0])
    assert all(row['adjusted']['macro_f1'] > row['baseline']['macro_f1'] for row in report.values())
    with pytest.raises(ValueError, match='coverage'):
        fit_policy(raw[:20], {key: value[:20] for key, value in data.items()})
    with pytest.raises(ValueError, match='all three'):
        fit_policy(np.tile([.4, .35, .25], (300, 1)), data)


def test_policy_preserves_other_market_horizon_and_relative_direction_scores():
    raw, data = examples()
    original = raw.copy()
    adjusted = apply_policy(raw, data['x'], 1, policy())
    np.testing.assert_array_equal(raw, original)
    np.testing.assert_allclose(adjusted[:, 0] / adjusted[:, 1], raw[:, 0] / raw[:, 1])
    np.testing.assert_allclose(adjusted.sum(axis=1), 1.)
    np.testing.assert_array_equal(apply_policy(raw, data['x'], 3, policy()), raw)
    data['x'][:, 19] = 1
    np.testing.assert_array_equal(apply_policy(raw, data['x'], 1, policy()), raw)


def test_loaded_model_policy_matches_batch_calibration():
    raw, data = examples()
    weights = np.zeros((21, 3))
    model = DirectionModel(np.zeros(20), np.ones(20), weights, 1, 1, 64)
    adjusted = wrap_model(model, policy())
    expected = apply_policy(model.probabilities(data['x']), data['x'], 1, policy())
    np.testing.assert_array_equal(adjusted.probabilities(data['x']), expected)
    assert set(adjusted.predict(data['x'])) == {2}
    assert not model.weights.any()


@pytest.fixture
def revision(bundle, tmp_path, monkeypatch):
    import core.arrow_flat as flat
    output, alias = bundle
    register_arrow(output, alias)
    # Synthetic corpus has few examples per liquidity group; real fitting is tested above.
    monkeypatch.setattr(flat, 'fit_policy', lambda *_: (policy(0.), {}))
    class Clock:
        @staticmethod
        def now(_tz): return datetime(2026, 9, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(flat, 'datetime', Clock)
    data = DirectionCorpus(output.parent / 'source', lookback=64)
    destination = tmp_path / 'revision'
    before = {name: file_hash(output / name) for name in ['selection.json', 'direction-1d.npz', 'direction-3d.npz']}
    build_revision(data, output, destination, date(2026, 10, 5), date(2026, 10, 3))
    assert before == {name: file_hash(output / name) for name in before}
    register_arrow(destination, alias)
    return output, alias, destination


def test_revision_has_new_identity_and_keeps_the_original_loadable(revision):
    original, alias, destination = revision
    old, new = Arrow.load(original), Arrow.load(alias)
    assert old.card['version'] != new.card['version']
    assert new.directory == destination
    assert new.info()['reserved_final_period'] == ['2026-10-05', '2026-11-02']
    assert new.info()['parent_version'] == old.card['version']
    assert old.info()['reserved_final_period'] == ['2026-10-03', '2026-10-31']
    assert new.predict(frame(), 'AAA', 'us')['direction'] == old.predict(frame(), 'AAA', 'us')['direction']
    with pytest.raises(ValueError, match='training/selection'):
        new.predict(frame(), 'AAA', 'us', as_of=date(2026, 10, 1))


def test_new_forward_test_accepts_appends_but_keeps_locked_prefix(revision, tmp_path):
    _, _, destination = revision
    newer = corpus(tmp_path / 'newer', end=date(2026, 11, 14))
    report = evaluate_revision(newer, destination, today=date(2026, 11, 14))
    assert report['period'] == ['2026-10-05', '2026-11-02']
    assert report['test_role'] == 'reserved_forward_after_feedback'
    assert report['selection_sha256'] == Arrow.load(destination).card['selection_sha256']
    with pytest.raises(ValueError, match='already consumed'):
        evaluate_revision(newer, destination, today=date(2026, 11, 14))


def test_incomplete_forward_test_does_not_consume_report(revision, tmp_path):
    _, _, destination = revision
    older = corpus(tmp_path / 'older', end=date(2026, 10, 3))
    with pytest.raises(ValueError, match='not yet available'):
        evaluate_revision(older, destination, today=date(2026, 10, 20))
    assert not (destination / 'final-evaluation.json').exists()


def test_calibration_report_is_integrity_checked(revision):
    _, _, destination = revision
    report = destination / 'flat-validation.json'
    report.write_text(json.dumps({'tampered': True}))
    with pytest.raises(ValueError, match='provenance'):
        Arrow.load(destination)
