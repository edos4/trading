"""Validation-fitted Flat scores for US one-day Arrow, with a fresh forward test."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil

import numpy as np

from core.kronos_direction import (
    DirectionConfig, DirectionModel, _data_summary, _examples, classification_metrics,
    direction_acceptance, evaluate_direction,
)
from core.kronos_weekly import PHT, exclusive_lock, write_json
from learn.kronos_direction_dataset import FEATURES, DirectionCorpus
from learn.kronos_weekly_dataset import expected_session, file_hash

THRESHOLDS = (250_000_000., 1_000_000_000.)
GROUPS = ('below_250m', '250m_to_1b', 'at_least_1b')


def source_hashes() -> dict[str, str]:
    return {'core/arrow_flat.py': file_hash(Path(__file__))}


def validate_policy(policy: dict) -> None:
    if (policy.get('method') != 'us_1d_liquidity_flat_bias_v1'
            or policy.get('turnover_thresholds_usd') != list(THRESHOLDS)
            or set(policy.get('biases', {})) != set(GROUPS)
            or not all(isinstance(v, (int, float)) and np.isfinite(v) and 0 <= v <= 1.5
                       for v in policy['biases'].values())):
        raise ValueError('Invalid Arrow Flat policy')


def apply_policy(probabilities: np.ndarray, features: np.ndarray, horizon: int, policy: dict) -> np.ndarray:
    """Boost only the Flat logit; retain the relative Up/Down evidence and ATR labels."""
    validate_policy(policy)
    result = np.array(probabilities, dtype=float, copy=True)
    if horizon != 1:
        return result
    us = features[:, FEATURES.index('ph_market')] == 0
    turnover = features[:, FEATURES.index('log_turnover')]
    group = np.searchsorted(np.log1p(THRESHOLDS), turnover, side='right')
    for index, name in enumerate(GROUPS):
        if not policy['biases'][name]:
            continue
        mask = us & (group == index)
        result[mask, 2] *= np.exp(policy['biases'][name])
        result[mask] /= result[mask].sum(axis=1, keepdims=True)
    return result


@dataclass
class FlatAwareModel(DirectionModel):
    policy: dict | None = None

    def probabilities(self, features: np.ndarray) -> np.ndarray:
        return apply_policy(super().probabilities(features), features, self.horizon, self.policy)


def wrap_model(model: DirectionModel, policy: dict) -> FlatAwareModel:
    validate_policy(policy)
    return FlatAwareModel(model.mean, model.scale, model.weights, model.horizon, model.best_epoch,
                          model.lookback, model.quality, model.feature_version, deepcopy(policy))


def fit_policy(probabilities: np.ndarray, examples: dict, *, min_samples: int = 100,
               min_class_samples: int = 10) -> tuple[dict, dict]:
    """Choose bounded offsets from earlier validation labels, never the reported live day."""
    if min_samples < 3 or min_class_samples < 1:
        raise ValueError('Calibration coverage floors must be positive')
    if (probabilities.shape != (len(examples['x']), 3) or not np.isfinite(probabilities).all()
            or (probabilities < 0).any() or not np.allclose(probabilities.sum(axis=1), 1)):
        raise ValueError('Invalid calibration probabilities')
    y = examples['y'][:, 0]
    turnover = examples['x'][:, FEATURES.index('log_turnover')]
    group = np.searchsorted(np.log1p(THRESHOLDS), turnover, side='right')
    us = examples['markets'] == 'us'
    if not np.array_equal(examples['x'][:, FEATURES.index('ph_market')], (~us).astype(float)):
        raise ValueError('Market features differ from validation market labels')
    biases, report = {}, {}
    for index, name in enumerate(GROUPS):
        mask = us & (group == index)
        actual, raw = y[mask], probabilities[mask]
        if len(actual) < min_samples or (np.bincount(actual, minlength=3) < min_class_samples).any():
            raise ValueError(f'{name}: insufficient historical calibration coverage')
        baseline = classification_metrics(actual, raw.argmax(axis=1))
        candidates = []
        for value in np.linspace(0, 1.5, 31):
            bias = round(float(value), 2)
            adjusted = raw * [1., 1., np.exp(bias)]
            metrics = classification_metrics(actual, adjusted.argmax(axis=1))
            if all(row['recall'] >= .1 for row in metrics['classes'].values()):
                candidates.append((metrics['macro_f1'], metrics['balanced_accuracy'], -bias, metrics))
        if not candidates:
            raise ValueError(f'{name}: no policy retains all three directions')
        best = max(candidates, key=lambda row: row[:3])
        if best[0] + 1e-12 < baseline['macro_f1']:
            raise ValueError(f'{name}: calibration regresses validation macro-F1')
        biases[name] = -best[2]
        report[name] = {'baseline': baseline, 'adjusted': best[3], 'flat_logit_bias': -best[2]}
    return {'method': 'us_1d_liquidity_flat_bias_v1', 'turnover_thresholds_usd': list(THRESHOLDS),
            'biases': biases}, report


def build_revision(corpus: DirectionCorpus, parent_dir: Path, output: Path, forward_start: date,
                   feedback_through: date) -> dict:
    """Keep raw checkpoints immutable, hash-lock a policy, and reserve new unseen dates."""
    from core.arrow import Arrow
    parent = Arrow.load(parent_dir)
    selected = parent.selection
    if 'flat_policy' in selected:
        raise ValueError('Calibrate from the original Arrow bundle, not an already calibrated revision')
    if forward_start <= feedback_through or forward_start < datetime.now(PHT).date():
        raise ValueError('Reserve a future period after the already observed feedback')
    if corpus.sha256 != selected['dataset_sha256']:
        raise ValueError('Calibration must use the original pre-feedback corpus')
    if (corpus.lookback != selected['lookback'] or asdict(corpus.quality) != selected['quality']
            or list(corpus.excluded_symbols) != selected['excluded_symbols']):
        raise ValueError('Calibration feature/quality policy differs from its parent')
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Arrow revision output must be empty')
    config = DirectionConfig(**selected['config'])
    cutoff = date.fromisoformat(selected['final_test_start'])
    if cutoff > feedback_through or forward_start <= cutoff:
        raise ValueError('Calibration dates must precede the fresh forward period')
    validation = _examples(corpus, cutoff - timedelta(weeks=config.validation_weeks), cutoff,
                           config.validation_samples, config.seed + 1000, config, training=False)
    raw = parent.models[1].probabilities(validation['x'])
    policy, cohorts = fit_policy(raw, validation)
    revised = wrap_model(parent.models[1], policy)
    baseline_report = evaluate_direction(parent.models[1], validation)
    adjusted_report = evaluate_direction(revised, validation)
    for market in ('us', 'ph'):
        if adjusted_report['model'][market]['macro_f1'] + 1e-12 < baseline_report['model'][market]['macro_f1']:
            raise ValueError(f'{market}: aggregate validation macro-F1 regresses')
    report = {'role': 'reused_historical_validation_not_forward_accuracy',
              'examples': _data_summary(validation), 'cohorts': cohorts,
              'baseline': baseline_report, 'adjusted': adjusted_report,
              'feedback_through': feedback_through.isoformat()}
    selection = deepcopy(selected)
    selection.update(flat_policy=policy, revision_source_files=source_hashes(),
                     history_verification_cutoff=cutoff.isoformat(),
                     final_test_start=forward_start.isoformat(),
                     final_test_end=(forward_start + timedelta(days=config.final_test_days)).isoformat(),
                     parent_selection_sha256=parent.card['selection_sha256'],
                     feedback_through=feedback_through.isoformat(),
                     calibration_report_sha256=None)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'flat-validation.json', report)
    selection['calibration_report_sha256'] = file_hash(output / 'flat-validation.json')
    for name in selection['models']:
        shutil.copyfile(parent.directory / name, output / name)
    write_json(output / 'selection.json', selection)
    (output / 'selection.sha256').write_text(file_hash(output / 'selection.json') + '\n')
    return selection


def evaluate_revision(corpus: DirectionCorpus, output: Path, *, today: date | None = None) -> dict:
    """Evaluate the revised decision rule once on its fresh forward period."""
    from core.arrow import _load_bundle
    from core.market import last_closed_session_date
    output = Path(output)
    with exclusive_lock(output / 'final-evaluation.lock'):
        if (output / 'final-evaluation.json').exists():
            raise ValueError('Final test was already consumed; use its saved report')
        selection, models, digest = _load_bundle(output)
        if 'flat_policy' not in selection:
            raise ValueError('Expected a Flat-calibrated Arrow revision')
        start, end = map(date.fromisoformat, (selection['final_test_start'], selection['final_test_end']))
        if end > (today or datetime.now(PHT).date()) or date.fromisoformat(corpus.meta['window_end']) < end:
            raise ValueError('The full reserved final period is not yet available')
        for market in ('us', 'ph'):
            expected = expected_session(end, market)
            if today is None and last_closed_session_date(market) < expected:
                raise ValueError(f'Final {market} session has not closed')
            latest = max(int(corpus.days[s['offset'] + s['count'] - 1])
                         for s in corpus.symbols if s['market'] == market)
            if latest < expected.toordinal():
                raise ValueError(f'Final {market} source is stale')
        if (asdict(corpus.quality) != selection['quality'] or corpus.lookback != selection['lookback']
                or list(corpus.excluded_symbols) != selection['excluded_symbols']):
            raise ValueError('Final evaluation policy differs from the locked selection')
        history, _ = corpus.history_hash(date.fromisoformat(selection['history_verification_cutoff']),
                                         [tuple(key) for key in selection['history_symbols']])
        if history != selection['history_sha256']:
            raise ValueError('Historical training/validation data changed')
        config = DirectionConfig(**selection['config'])
        examples = _examples(corpus, start, end, config.test_samples, config.seed + 3000,
                             config, training=False, embargo=False)
        report = {'test_role': 'reserved_forward_after_feedback', 'selection_sha256': digest,
                  'dataset_sha256': corpus.sha256, 'period': [start.isoformat(), end.isoformat()],
                  'examples': _data_summary(examples),
                  'horizons': {str(h): evaluate_direction(model, examples) for h, model in models.items()}}
        report['failures'] = direction_acceptance(report['horizons'], config)
        report['passed_gates'] = not report['failures']
        write_json(output / 'final-evaluation.json', report)
        return report
