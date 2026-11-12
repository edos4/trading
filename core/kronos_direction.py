"""Small three-class direction models with chronological, locked evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from core.kronos_weekly import exclusive_lock, write_json
from learn.kronos_direction_dataset import (
    FEATURES, FEATURE_VERSION, LABELS, DirectionCorpus, QualityPolicy,
    context_features, quality_masks,
)
from learn.kronos_weekly_dataset import file_hash
from utils.logger import log


@dataclass(frozen=True)
class DirectionConfig:
    folds: int = 3
    validation_weeks: int = 8
    test_weeks: int = 4
    final_test_days: int = 28
    gap_sessions: int = 3
    train_samples: int = 32_000
    validation_samples: int = 6_000
    test_samples: int = 6_000
    min_eval_samples: int = 50
    min_eval_symbols: int = 10
    min_class_samples: int = 5
    epochs: int = 30
    batch_size: int = 512
    learning_rate: float = .01
    l2: float = .001
    seed: int = 42
    half_life_days: float = 730.
    uniform_share: float = .1
    class_weight: str = 'balanced'
    selection_metric: str = 'macro_f1'

    def __post_init__(self) -> None:
        for name in ('folds', 'validation_weeks', 'test_weeks', 'final_test_days',
                     'train_samples', 'validation_samples', 'test_samples', 'epochs', 'batch_size'):
            if getattr(self, name) < (2 if name.endswith('samples') else 1):
                raise ValueError(f'{name} must be positive and provide both markets')
        if min(self.min_eval_samples, self.min_eval_symbols, self.min_class_samples) < 1:
            raise ValueError('Evaluation coverage floors must be positive')
        if self.gap_sessions < 3:
            raise ValueError('Embargo must cover the maximum three-session target')
        if (not np.isfinite(self.learning_rate) or self.learning_rate <= 0
                or not np.isfinite(self.l2) or self.l2 < 0
                or not np.isfinite(self.half_life_days) or self.half_life_days <= 0
                or not np.isfinite(self.uniform_share) or not 0 < self.uniform_share <= 1):
            raise ValueError('Invalid optimizer or recency setting')
        if self.class_weight not in ('none', 'balanced'):
            raise ValueError('Unknown class weighting')
        if self.selection_metric not in ('macro_f1', 'balanced_accuracy'):
            raise ValueError('Checkpoint selection must use direction metrics')


def classification_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    """Precision/recall/F1 and accuracy retain up, down and flat explicitly."""
    actual, predicted = np.asarray(actual, dtype=int), np.asarray(predicted, dtype=int)
    if actual.shape != predicted.shape or actual.ndim != 1 or not len(actual):
        raise ValueError('Require nonempty matching direction labels')
    if ((actual < 0) | (actual > 2) | (predicted < 0) | (predicted > 2)).any():
        raise ValueError('Unknown direction class')
    matrix = np.zeros((3, 3), dtype=np.int64)
    np.add.at(matrix, (actual, predicted), 1)
    support, predicted_count = matrix.sum(axis=1), matrix.sum(axis=0)
    hits = matrix.diagonal()
    precision = np.divide(hits, predicted_count, out=np.zeros(3), where=predicted_count > 0)
    recall = np.divide(hits, support, out=np.zeros(3), where=support > 0)
    f1 = np.divide(2 * precision * recall, precision + recall, out=np.zeros(3), where=precision + recall > 0)
    return {
        'samples': len(actual), 'accuracy': float(hits.sum() / len(actual)),
        'balanced_accuracy': float(recall[support > 0].mean()), 'macro_f1': float(f1.mean()),
        'up_down_precision': float(precision[:2].mean()),
        'classes': {name: {'precision': float(precision[i]), 'recall': float(recall[i]),
                           'f1': float(f1[i]), 'support': int(support[i]),
                           'predicted': int(predicted_count[i])} for i, name in enumerate(LABELS)},
        'confusion': {name: {other: int(matrix[i, j]) for j, other in enumerate(LABELS)}
                      for i, name in enumerate(LABELS)},
    }


def market_metrics(actual: np.ndarray, predicted: np.ndarray, markets: np.ndarray) -> dict:
    result = {}
    for market in ('us', 'ph'):
        mask = markets == market
        if not mask.any():
            raise ValueError(f'Missing {market} evaluation examples')
        result[market] = classification_metrics(actual[mask], predicted[mask])
    return result


def selection_score(metrics: dict, primary: str) -> tuple[float, float, float]:
    """Equal market weights; raw accuracy cannot win by predicting only flat."""
    return tuple(float(np.mean([metrics[m][name] for m in ('us', 'ph')]))
                 for name in (primary, 'balanced_accuracy' if primary == 'macro_f1' else 'macro_f1',
                              'up_down_precision'))


@dataclass
class DirectionModel:
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray
    horizon: int
    best_epoch: int
    lookback: int
    quality: QualityPolicy = QualityPolicy()
    feature_version: str = FEATURE_VERSION

    def probabilities(self, features: np.ndarray) -> np.ndarray:
        features = np.asarray(features, dtype=float)
        if features.ndim != 2 or features.shape[1] != len(FEATURES) or not np.isfinite(features).all():
            raise ValueError('Invalid direction features')
        x = np.column_stack((np.clip((features - self.mean) / self.scale, -8, 8), np.ones(len(features))))
        logits = x @ self.weights
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        return probabilities / probabilities.sum(axis=1, keepdims=True)

    def predict(self, features: np.ndarray) -> np.ndarray:
        return self.probabilities(features).argmax(axis=1)

    def save(self, path: Path) -> None:
        np.savez(path, mean=self.mean, scale=self.scale, weights=self.weights,
                 horizon=self.horizon, best_epoch=self.best_epoch,
                 lookback=self.lookback,
                 quality_json=json.dumps(asdict(self.quality), sort_keys=True),
                 feature_version=self.feature_version, feature_names=np.asarray(FEATURES))

    @classmethod
    def load(cls, path: Path) -> 'DirectionModel':
        with np.load(path, allow_pickle=False) as saved:
            if str(saved['feature_version']) != FEATURE_VERSION or tuple(saved['feature_names']) != FEATURES:
                raise ValueError('Direction feature contract differs')
            model = cls(saved['mean'].copy(), saved['scale'].copy(), saved['weights'].copy(),
                        int(saved['horizon']), int(saved['best_epoch']), int(saved['lookback']),
                        quality=QualityPolicy(**json.loads(str(saved['quality_json']))))
        if (model.mean.shape != (len(FEATURES),) or model.scale.shape != model.mean.shape
                or model.weights.shape != (len(FEATURES) + 1, 3)
                or model.horizon not in (1, 3) or model.best_epoch < 1 or model.lookback < 61 or (model.scale <= 0).any()
                or not all(np.isfinite(v).all() for v in (model.mean, model.scale, model.weights))):
            raise ValueError('Malformed direction model')
        return model


def fit_direction(train: dict, validation: dict, horizon: int, config: DirectionConfig) -> tuple[DirectionModel, dict]:
    """Train weighted softmax directly against the actual three-class targets."""
    if horizon not in (1, 3):
        raise ValueError('Only one- and three-day direction targets are supported')
    column = 0 if horizon == 1 else 1
    if train['dates'][:, 1].max() >= validation['dates'][:, 0].min():
        raise ValueError('Training labels overlap validation dates')
    x, y = train['x'], train['y'][:, column]
    mean, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-6)
    normalized = np.column_stack((np.clip((x - mean) / scale, -8, 8), np.ones(len(x))))
    rng = np.random.default_rng(config.seed + horizon)
    weights = rng.normal(0., .01, size=(normalized.shape[1], 3))
    first, second = np.zeros_like(weights), np.zeros_like(weights)
    counts = np.bincount(y, minlength=3)
    if (counts == 0).any():
        raise ValueError('Training split must include up, down and flat')
    class_weights = len(y) / (3 * counts) if config.class_weight == 'balanced' else np.ones(3)
    best, best_score, history, step = None, None, [], 0
    for epoch in range(config.epochs):
        order = rng.permutation(len(x))
        for begin in range(0, len(x), config.batch_size):
            indices = order[begin:begin + config.batch_size]
            xb, yb = normalized[indices], y[indices]
            logits = xb @ weights
            logits -= logits.max(axis=1, keepdims=True)
            probabilities = np.exp(logits)
            probabilities /= probabilities.sum(axis=1, keepdims=True)
            probabilities[np.arange(len(yb)), yb] -= 1
            sample_weights = class_weights[yb]
            gradient = xb.T @ (probabilities * sample_weights[:, None]) / sample_weights.sum()
            gradient[:-1] += config.l2 * weights[:-1]
            step += 1
            first = .9 * first + .1 * gradient
            second = .999 * second + .001 * gradient ** 2
            weights -= config.learning_rate * (first / (1 - .9 ** step)) / (np.sqrt(second / (1 - .999 ** step)) + 1e-8)
        if not np.isfinite(weights).all():
            raise ValueError('Nonfinite direction weights')
        model = DirectionModel(mean, scale, weights.copy(), horizon, epoch + 1, train['lookback'],
                               quality=QualityPolicy(**train.get('quality', asdict(QualityPolicy()))))
        metrics = market_metrics(validation['y'][:, column], model.predict(validation['x']), validation['markets'])
        score = selection_score(metrics, config.selection_metric)
        history.append({'epoch': epoch + 1, 'validation': metrics, 'selection_score': score})
        if best_score is None or score > best_score:
            best, best_score = model, score
    if best is None:
        raise ValueError('No selected direction checkpoint')
    return best, {'best_epoch': best.best_epoch, 'selection_score': best_score,
                  'class_weights': class_weights.tolist(), 'history': history,
                  'normalization_source': 'training examples only'}


def _examples(corpus: DirectionCorpus, lower: date, upper: date, budget: int, seed: int,
              config: DirectionConfig, *, training: bool, recent: bool = False, embargo: bool = True) -> dict:
    pool = corpus.pool(lower, upper, gap_sessions=config.gap_sessions if embargo else 0,
                       half_life_days=config.half_life_days if training and recent else 0,
                       uniform_share=config.uniform_share)
    result = corpus.examples(pool.sample(budget, seed, training=training))
    if result['dates'][:, 0].min() < lower.toordinal() or result['dates'][:, 1].max() >= upper.toordinal():
        raise ValueError('A forecast target escaped its chronological split')
    return result


def evaluate_direction(model: DirectionModel, examples: dict) -> dict:
    column = 0 if model.horizon == 1 else 1
    actual = examples['y'][:, column]
    # Baselines are fixed before test labels are read. Persistence uses the
    # completed context's return at the same horizon, never a future return.
    lag = 0 if model.horizon == 1 else 1
    from core.kronos_confirm import classify
    persistence = np.asarray([LABELS.index(classify(r, band * 100))
                              for r, band in zip(examples['x'][:, lag], examples['bands'])])
    return {'coverage': {m: {'symbols': len(np.unique(examples['targets'][examples['markets'] == m, 1]))}
                         for m in ('us', 'ph')},
            'model': market_metrics(actual, model.predict(examples['x']), examples['markets']),
            'always_flat': market_metrics(actual, np.full(len(actual), 2), examples['markets']),
            'persistence': market_metrics(actual, persistence, examples['markets'])}


def direction_acceptance(horizons: dict, config: DirectionConfig) -> list[str]:
    """Research gates require usable classes and improvement over simple baselines."""
    failures = []
    for horizon, result in horizons.items():
        for market in ('us', 'ph'):
            model = result['model'][market]
            tag = f'{market}_{horizon}d'
            if (model['samples'] < config.min_eval_samples
                    or result['coverage'][market]['symbols'] < config.min_eval_symbols
                    or any(v['support'] < config.min_class_samples for v in model['classes'].values())):
                failures.append(f'{tag}: insufficient final coverage')
            for metric in ('balanced_accuracy', 'macro_f1'):
                floor = max(result[name][market][metric] for name in ('always_flat', 'persistence'))
                if model[metric] <= floor + 1e-12:
                    failures.append(f'{tag}: does not improve baseline {metric}')
    return failures


def _data_summary(examples: dict) -> dict:
    return {'samples': len(examples['x']),
            'markets': {m: int((examples['markets'] == m).sum()) for m in ('us', 'ph')},
            'first_target': date.fromordinal(int(examples['dates'][:, 0].min())).isoformat(),
            'last_target': date.fromordinal(int(examples['dates'][:, 1].max())).isoformat()}


def run_direction_experiment(corpus: DirectionCorpus, output: Path, final_test_start: date,
                             config: DirectionConfig = DirectionConfig(), *, kronos_evaluation: Path | None = None) -> dict:
    """Serialize starts for the same destination without touching active models."""
    output = Path(output)
    with exclusive_lock(output.parent / f'.{output.name}.lock'):
        return _run_direction_experiment(corpus, output, final_test_start, config, kronos_evaluation)


def _implementation_hashes() -> dict:
    root = Path(__file__).resolve().parents[1]
    return {name: file_hash(root / name) for name in
            ('core/kronos_direction.py', 'learn/kronos_direction_dataset.py',
             'core/kronos_direction_eval.py', 'core/kronos_confirm.py', 'core/market.py',
             'learn/kronos_weekly_dataset.py', 'indicators/_math.py')}


def _run_direction_experiment(corpus: DirectionCorpus, output: Path, final_test_start: date,
                              config: DirectionConfig, kronos_evaluation: Path | None) -> dict:
    """Development walk-forward tests never select parameters; final stays closed."""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Direction experiment output must be empty')
    if final_test_start > date.fromisoformat(corpus.meta['window_end']):
        raise ValueError('Training snapshot must reach the reserved final-test start')
    if final_test_start <= date.fromisoformat(corpus.meta['window_start']):
        raise ValueError('No history precedes the reserved final test')
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    configuration = {'config': asdict(config), 'quality': asdict(corpus.quality),
                     'excluded_symbols': corpus.excluded_symbols, 'lookback': corpus.lookback,
                     'feature_version': FEATURE_VERSION, 'feature_names': FEATURES,
                     'dataset_sha256': corpus.sha256, 'final_test_start': final_test_start.isoformat(),
                     'kronos_evaluation_sha256': file_hash(kronos_evaluation) if kronos_evaluation else None,
                     'final_test_end': (final_test_start + timedelta(days=config.final_test_days)).isoformat()}
    write_json(output / 'configuration.json', configuration)
    write_json(output / 'quality-report.json', corpus.report)
    folds = []
    scores = {h: {variant: [] for variant in ('uniform', 'recent')} for h in (1, 3)}
    for index in range(config.folds):
        end = final_test_start - timedelta(weeks=config.test_weeks * (config.folds - index - 1))
        test_start = end - timedelta(weeks=config.test_weeks)
        validation_start = test_start - timedelta(weeks=config.validation_weeks)
        log.info(f'Direction | fold {index + 1}/{config.folds}: development test {test_start} to {end}')
        validation = _examples(corpus, validation_start, test_start, config.validation_samples,
                               config.seed + 100 + index, config, training=False)
        test = _examples(corpus, test_start, end, config.test_samples, config.seed + 200 + index,
                         config, training=False, embargo=False)
        fold = {'fold': index + 1, 'train_upper_boundary': validation_start.isoformat(),
                'validation': _data_summary(validation), 'development_test': _data_summary(test), 'variants': {}}
        for variant in ('uniform', 'recent'):
            train = _examples(corpus, date.min, validation_start, config.train_samples,
                              config.seed + index, config, training=True, recent=variant == 'recent')
            fold['variants'][variant] = {'training': _data_summary(train), 'horizons': {}}
            for horizon in (1, 3):
                model, training = fit_direction(train, validation, horizon, config)
                scores[horizon][variant].append(training['selection_score'])
                fold['variants'][variant]['horizons'][str(horizon)] = {
                    'training': training, 'development_test': evaluate_direction(model, test)}
                if kronos_evaluation:
                    from core.kronos_direction_eval import compare_saved_kronos
                    fold['variants'][variant]['horizons'][str(horizon)]['saved_kronos_comparison'] = compare_saved_kronos(
                        corpus, model, kronos_evaluation, test_start, end)
        folds.append(fold)
        write_json(output / 'development-folds.json', {'folds': folds, 'test_role': 'development_only'})
    # Variant choice uses validation scores exclusively, never development-test results.
    chosen = {h: max(('uniform', 'recent'), key=lambda variant: tuple(np.mean(scores[h][variant], axis=0))) for h in (1, 3)}
    validation_start = final_test_start - timedelta(weeks=config.validation_weeks)
    validation = _examples(corpus, validation_start, final_test_start, config.validation_samples,
                           config.seed + 1000, config, training=False)
    final_training, checkpoint_hashes = {}, {}
    for variant in sorted(set(chosen.values())):
        train = _examples(corpus, date.min, validation_start, config.train_samples, config.seed + 2000,
                          config, training=True, recent=variant == 'recent')
        for horizon in (h for h in (1, 3) if chosen[h] == variant):
            model, training = fit_direction(train, validation, horizon, config)
            path = output / f'direction-{horizon}d.npz'
            model.save(path)
            checkpoint_hashes[path.name] = file_hash(path)
            final_training[str(horizon)] = {'sampling': variant, 'training': training,
                                            'training_examples': _data_summary(train),
                                            'validation': evaluate_direction(model, validation)}
    history_hash, history_symbols = corpus.history_hash(final_test_start)
    selection = {'schema_version': 1, **configuration, 'chosen_sampling': {str(h): v for h, v in chosen.items()},
                 'models': checkpoint_hashes, 'history_sha256': history_hash,
                 'history_symbols': history_symbols, 'selected_using': 'validation_only',
                 'source_files': _implementation_hashes(),
                 'training': final_training, 'elapsed_seconds': time.perf_counter() - started,
                 'final_test_status_at_selection': 'reserved'}
    write_json(output / 'selection.json', selection)
    (output / 'selection.sha256').write_text(file_hash(output / 'selection.json') + '\n')
    log.info(f'Direction | models locked; final test reserved for {selection["final_test_start"]} to {selection["final_test_end"]}')
    return selection


def evaluate_final(corpus: DirectionCorpus, output: Path, *, today: date | None = None) -> dict:
    """One-shot final evaluation of hash-locked models against later source data."""
    output = Path(output)
    with exclusive_lock(output / 'final-evaluation.lock'):
        if (output / 'final-evaluation.json').exists():
            raise ValueError('Final test was already consumed; use its saved report')
        selection_path = output / 'selection.json'
        selection_bytes = selection_path.read_bytes()
        if hashlib.sha256(selection_bytes).hexdigest() != (output / 'selection.sha256').read_text().strip():
            raise ValueError('Locked selection manifest differs')
        selection = json.loads(selection_bytes)
        if selection['source_files'] != _implementation_hashes():
            raise ValueError('Direction implementation differs from the locked selection')
        start, end = date.fromisoformat(selection['final_test_start']), date.fromisoformat(selection['final_test_end'])
        from core.kronos_weekly import PHT
        from datetime import datetime
        current = today or datetime.now(PHT).date()
        if end > current or date.fromisoformat(corpus.meta['window_end']) < end:
            raise ValueError('The full reserved final period is not yet available')
        from learn.kronos_weekly_dataset import expected_session
        for market in ('us', 'ph'):
            expected = expected_session(end, market)
            if today is None:
                from core.market import last_closed_session_date
                if last_closed_session_date(market) < expected:
                    raise ValueError(f'Final {market} session has not closed')
            latest = max(int(corpus.days[s['offset'] + s['count'] - 1])
                         for s in corpus.symbols if s['market'] == market)
            if latest < expected.toordinal():
                raise ValueError(f'Final {market} source is stale')
        if (asdict(corpus.quality) != selection['quality'] or corpus.lookback != selection['lookback']
                or list(corpus.excluded_symbols) != selection['excluded_symbols']):
            raise ValueError('Final evaluation policy differs from the locked selection')
        history_hash, _ = corpus.history_hash(start, [tuple(key) for key in selection['history_symbols']])
        if history_hash != selection['history_sha256']:
            raise ValueError('Historical training/validation data changed')
        for name, digest in selection['models'].items():
            if name not in ('direction-1d.npz', 'direction-3d.npz') or file_hash(output / name) != digest:
                raise ValueError('Locked direction checkpoint differs')
        config = DirectionConfig(**selection['config'])
        examples = _examples(corpus, start, end, config.test_samples, config.seed + 3000,
                             config, training=False, embargo=False)
        report = {'test_role': 'reserved_final', 'selection_sha256': hashlib.sha256(selection_bytes).hexdigest(),
                  'dataset_sha256': corpus.sha256, 'period': [start.isoformat(), end.isoformat()],
                  'examples': _data_summary(examples), 'horizons': {}}
        for horizon in (1, 3):
            model = DirectionModel.load(output / f'direction-{horizon}d.npz')
            if model.horizon != horizon:
                raise ValueError('Checkpoint horizon differs')
            report['horizons'][str(horizon)] = evaluate_direction(model, examples)
        report['failures'] = direction_acceptance(report['horizons'], config)
        report['passed_gates'] = not report['failures']
        write_json(output / 'final-evaluation.json', report)
        return report


def predict_direction(model: DirectionModel, frame: pd.DataFrame, market: str,
                      quality: QualityPolicy | None = None) -> dict:
    """Inference uses the same causal features and quality rules as training."""
    from learn.kronos_weekly_dataset import single_scale_mask, suspension_breaks
    if quality is not None and quality != model.quality:
        raise ValueError('Inference policy differs from the trained model')
    quality = model.quality
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError('Inference requires dated OHLCV bars')
    work = frame.sort_index(kind='stable')
    work = work[~work.index.duplicated(keep='last')].tail(model.lookback)
    if len(work) != model.lookback or work.index.isna().any():
        raise ValueError('Inference context differs from the trained lookback')
    context = work[['open', 'high', 'low', 'close', 'volume']].to_numpy(dtype=float)
    features, band = context_features(context, market)
    masks = quality_masks(context, len(context), market, quality)
    failures = [name for name, mask in masks.items() if bool(mask[0])]
    days = np.asarray([stamp.date().toordinal() for stamp in work.index])
    if suspension_breaks(days, market)[1]:
        failures.append('long_gap')
    if not single_scale_mask(context[:, 3], model.lookback)[0]:
        failures.append('mixed_price_scale')
    if failures:
        return {'direction': None, 'abstained': True, 'reasons': failures}
    probability = model.probabilities(features[None])[0]
    return {'direction': LABELS[int(probability.argmax())], 'abstained': False,
            'probabilities': {label: float(probability[i]) for i, label in enumerate(LABELS)},
            'flat_band_return': band, 'horizon': model.horizon}
