"""Frozen, market-stratified weekly evaluation and automatic promotion gates."""
from __future__ import annotations

from datetime import date
import gc
import math
from pathlib import Path
import zlib

import numpy as np
import pandas as pd

from learn import kronos_dataset as kd

DIRECTION_SCORING = 'atr-up-down-flat-v1'


def select_anchors(dataset: Path, *, symbols_per_market: int = 50, lookback: int = 400) -> list[dict]:
    """Select anchors without using future returns or cross-currency rankings."""
    feats, days, meta = kd.load_corpus(dataset)
    lower = date.fromisoformat(meta['val_end']).toordinal()
    result = []
    for market in ('us', 'ph'):
        ranked = []
        for symbol in meta['symbols']:
            if symbol['market'] != market:
                continue
            offset, count = symbol['offset'], symbol['count']
            ordinals = days[offset:offset + count]
            first = max(lookback, int(np.searchsorted(ordinals, lower)))
            if count - first < 3:
                continue
            # Rank using only information preceding the evaluation interval.
            past = feats[offset + max(0, first - 20):offset + first]
            ranked.append((float(np.mean(past[:, 3] * past[:, 4], dtype=np.float64)), symbol, first))
        ranked.sort(key=lambda row: (row[0], row[1]['name']))
        if ranked:
            indices = sorted(set(np.linspace(0, len(ranked) - 1, min(symbols_per_market, len(ranked)), dtype=int).tolist()))
            for index in indices:
                _, symbol, first = ranked[index]
                offset, count = symbol['offset'], symbol['count']
                for target in range(first, count - 2):
                    # Exclude suspended contexts using the same legal range logic.
                    from learn.kronos_weekly_dataset import ranges_for_days
                    legal = ranges_for_days(days[offset:offset + count], offset,
                                            int(days[offset + target]), int(days[offset + target + 2]) + 1,
                                            lookback, 3, market)
                    if not any(start <= offset + target - lookback < start + n for start, n in legal):
                        continue
                    result.append({'market': market, 'symbol': symbol['name'],
                                   'context_start': offset + target - lookback,
                                   'target_start': offset + target, 'horizon': 3})
    return result


def _seed_predictor(predictor, text: str) -> None:
    value = zlib.crc32(text.encode()) & 0x7fffffff
    if hasattr(predictor, 'set_seed'):
        predictor.set_seed(value)
    else:
        import torch
        torch.manual_seed(value)


def _json_number(value: float) -> float | None:
    """Keep only finite JSON numbers; a failed forecast has no numeric result."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _credible_return(close: float, anchor: float) -> float | None:
    """A sample the live scanner can vote on, or nothing."""
    from core.kronos_confirm import MAX_CREDIBLE_RETURN
    if not math.isfinite(close) or close <= 0 or not math.isfinite(anchor) or anchor <= 0:
        return None
    value = close / anchor - 1
    if not math.isfinite(value) or value <= -1 or abs(value) > MAX_CREDIBLE_RETURN:
        return None
    return value


def _score_row(anchor: dict, horizon: int, actual: float, forecast: float, *, error: str | None,
               band: float, preparation, path: str, abstained: bool = False, **extra) -> dict:
    from core.kronos_confirm import MAX_CREDIBLE_RETURN
    actual_number = _json_number(actual)
    finite = math.isfinite(forecast)
    valid = (not abstained and error is None and finite and actual_number is not None
             and forecast > -1 and abs(forecast) <= MAX_CREDIBLE_RETURN)
    if abstained:
        reason = 'no credible sample'
    elif valid:
        reason = None
    else:
        reason = error or 'nonpositive, nonfinite, or beyond scanner credibility bound'
    return {**anchor, 'horizon': horizon, 'actual_return': actual_number,
            'forecast_return': forecast if valid else None, 'raw_forecast_return': forecast if finite else None,
            'valid': valid, 'abstained': abstained, 'error': error, 'forecast_path': path,
            'invalid_reason': reason,
            'sideways_band_return': _json_number(band), 'model_context': preparation.evidence() if preparation else None, **extra}


def evaluate_model(predictor, dataset: Path, anchors: list[dict], *, seed: int = 42) -> dict:
    """Score the scanner's 1-day votes and the evaluator's 3-day forecast.

    One-day uses three stochastic draws from one checkpoint. Three-day uses one
    evaluator call. Labels are the next stored sessions; each path supplies
    the timestamps that path uses in production.
    """
    from core.kronos_confirm import atr_pct_from_frame, sideways_band_pct
    from core.kronos_eval import WEEK_AHEAD, _prep_3d_frame
    from core.kronos_input import prepare_context
    from scripts.kronos_confirm_worker import forecast_returns
    feats, days, _ = kd.load_corpus(dataset)
    rows = []
    for anchor in anchors:
        a, b = anchor['context_start'], anchor['target_start']
        context = pd.DataFrame(np.array(feats[a:b]), columns=kd.FEATURE_COLS,
                               index=pd.to_datetime([date.fromordinal(int(d)) for d in days[a:b]]))
        ohlcv = context.loc[:, list(kd.FEATURE_COLS[:5])].copy()
        lookback = b - a
        price = float(ohlcv.close.iloc[-1])
        band = sideways_band_pct(atr_pct_from_frame(ohlcv, price)) / 100
        preparation = None
        try:
            preparation = prepare_context(ohlcv, lookback)
        except ValueError:
            preparation = None
        stamp = f"{seed}:{anchor['market']}:{anchor['symbol']}:{int(days[b - 1])}"
        try:
            confirmed = forecast_returns(predictor, anchor['symbol'], ohlcv, 3, lookback)
            returns = [float(value) for value in confirmed['returns']]
            # The worker rounds its reported anchor for display. Labels must
            # use the same unrounded close as its forecast-return denominator.
            anchor_close = price
            actual = float(feats[b, 3]) / anchor_close - 1 if math.isfinite(anchor_close) and anchor_close > 0 else float('nan')
            extra = {'rejected_samples': int(confirmed['rejected']), 'confirmation_samples': len(returns)}
            if not returns:
                rows.append(_score_row(anchor, 1, actual, float('nan'), error=None, band=band,
                                       preparation=preparation, path='confirmation', abstained=True, **extra))
            elif not math.isfinite(anchor_close) or anchor_close <= 0:
                raise ValueError('confirmation anchor close is not positive')
            else:
                rows.append(_score_row(anchor, 1, actual, float(np.mean(returns)), error=None, band=band,
                                       preparation=preparation, path='confirmation', **extra))
        except Exception as exc:
            rows.append(_score_row(anchor, 1, float('nan'), float('nan'), error=f'{type(exc).__name__}: {exc}',
                                   band=band, preparation=preparation, path='confirmation'))
        try:
            prepared = _prep_3d_frame(ohlcv, lookback)
            if prepared is None:
                raise ValueError('3-day evaluator rejected the context')
            x_df, x_timestamp, y_timestamp, anchor_close, _used = prepared
            actual = float(feats[b + WEEK_AHEAD - 1, 3]) / float(anchor_close) - 1
            returns = []
            for sample in range(3):
                _seed_predictor(predictor, f'{stamp}:3d:{sample}')
                predicted = predictor.predict(
                    df=x_df, x_timestamp=x_timestamp, y_timestamp=y_timestamp, pred_len=WEEK_AHEAD,
                    T=1.0, top_p=.9, sample_count=1, verbose=False)['close'].to_numpy(dtype=float)
                if len(predicted) != WEEK_AHEAD:
                    raise ValueError('Incomplete 3-day forecast')
                value = _credible_return(float(predicted[WEEK_AHEAD - 1]), float(anchor_close))
                if value is not None:
                    returns.append(value)
            extra = {'rejected_samples': 3 - len(returns), 'confirmation_samples': len(returns)}
            if not returns:
                rows.append(_score_row(anchor, 3, actual, float('nan'), error=None, band=band,
                                       preparation=preparation, path='evaluator', abstained=True, **extra))
            else:
                rows.append(_score_row(anchor, 3, actual, float(np.mean(returns)), error=None, band=band,
                                       preparation=preparation, path='evaluator', **extra))
        except Exception as exc:
            rows.append(_score_row(anchor, 3, float('nan'), float('nan'), error=f'{type(exc).__name__}: {exc}',
                                   band=band, preparation=preparation, path='evaluator'))
    return {'metrics': summarize(rows), 'rows': rows}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    number = float(np.mean(values))
    return number if math.isfinite(number) else None


def summarize(rows: list[dict]) -> dict:
    from core.kronos_confirm import classify
    result = {}
    for market in ('us', 'ph'):
        for horizon in (1, 3):
            subset = [r for r in rows if r['market'] == market and r['horizon'] == horizon]
            abstained = [r for r in subset if r.get('abstained')]
            scored = [r for r in subset if not r.get('abstained')]
            valid = [r for r in scored if r['valid']]
            directions = []
            confusion = {actual: {forecast: 0 for forecast in ('up', 'down', 'flat')}
                         for actual in ('up', 'down', 'flat')}
            for row in valid:
                band = row.get('sideways_band_return')
                if not isinstance(band, (int, float)) or not math.isfinite(band) or band <= 0:
                    raise ValueError('Direction scoring requires a finite positive sideways band')
                actual = classify(row['actual_return'], band * 100)
                forecast = classify(row['forecast_return'], band * 100)
                directions.append((actual, forecast))
                confusion[actual][forecast] += 1
            result[f'{market}_{horizon}d'] = {
                'symbols': len({r['symbol'] for r in subset}), 'windows': len(subset),
                'abstained': len(abstained),
                'invalid': len(scored) - len(valid),
                'invalid_rate': (len(scored) - len(valid)) / len(subset) if subset else 1.,
                'valid_windows': len(valid), 'valid_symbols': len({r['symbol'] for r in valid}),
                'direction_accuracy': _mean([float(actual == forecast) for actual, forecast in directions]),
                'direction_scoring': DIRECTION_SCORING,
                'direction_confusion': confusion,
                'mean_absolute_return_error': _mean([abs(r['actual_return'] - r['forecast_return']) for r in valid]),
                'sideways_fraction': _mean([float(forecast == 'flat') for _, forecast in directions]),
                'sideways_rule': 'ATR-scaled scanner band',
            }
    return result


def acceptance(candidate: dict, baseline: dict, *, min_symbols: int = 25, min_windows: int = 50,
               direction_tolerance: float = .02, error_tolerance: float = .10,
               invalid_policy: str = 'zero', max_invalid_rate: float = .01) -> list[str]:
    if invalid_policy not in ('zero', 'relative') or not 0 <= max_invalid_rate <= 1:
        raise ValueError('Invalid forecast acceptance policy')
    failures = []
    for key in ('us_1d', 'us_3d', 'ph_1d', 'ph_3d'):
        for tag, source in (('candidate', candidate), ('base', baseline)):
            metric = source.get(key, {})
            if metric.get('symbols', 0) < min_symbols or metric.get('windows', 0) < min_windows:
                failures.append(f'{key}: insufficient {tag} coverage')
            if invalid_policy == 'zero' and tag == 'candidate' and metric.get('invalid', 0):
                failures.append(f'{key}: invalid {tag} forecasts')
            for name in ('direction_accuracy', 'mean_absolute_return_error'):
                if not isinstance(metric.get(name), (int, float)) or not math.isfinite(metric[name]):
                    failures.append(f'{key}: nonfinite {tag} {name}')
            usable = metric.get('comparison_windows', metric.get('valid_windows', metric.get('windows',0)-metric.get('invalid',0)))
            usable_symbols = metric.get('comparison_symbols', metric.get('valid_symbols',metric.get('symbols',0)))
            if usable < min_windows or usable_symbols < min_symbols:
                failures.append(f'{key}: insufficient usable {tag} comparison coverage')
        c, b = candidate.get(key, {}), baseline.get(key, {})
        if invalid_policy == 'relative':
            c_rate = c.get('invalid', 0) / max(1, c.get('windows', 0))
            b_rate = b.get('invalid', 0) / max(1, b.get('windows', 0))
            if c_rate > max_invalid_rate or c_rate > b_rate:
                failures.append(f'{key}: candidate invalid rate exceeds cap or base')
            if c.get('valid_windows', c.get('windows', 0) - c.get('invalid', 0)) < min_windows:
                failures.append(f'{key}: insufficient valid candidate coverage')
            if c.get('valid_symbols', c.get('symbols', 0)) < min_symbols:
                failures.append(f'{key}: insufficient valid candidate symbols')
        if c.get('direction_accuracy') is not None and b.get('direction_accuracy') is not None:
            if c['direction_accuracy'] < b['direction_accuracy'] - direction_tolerance:
                failures.append(f'{key}: direction regression')
        if c.get('mean_absolute_return_error') is not None and b.get('mean_absolute_return_error') is not None:
            if c['mean_absolute_return_error'] > b['mean_absolute_return_error'] * (1 + error_tolerance):
                failures.append(f'{key}: return-error regression')
    return failures


def pair_comparison(base: dict, candidate: dict) -> None:
    """Score skill on identical usable anchors; retain independent invalid rates."""
    def identity(row):
        return row['market'],row['symbol'],row['target_start'],row['horizon']
    common={identity(r) for r in base['rows'] if r['valid']} & {identity(r) for r in candidate['rows'] if r['valid']}
    for result in (base,candidate):
        paired=summarize([r for r in result['rows'] if identity(r) in common])
        for key,metrics in result['metrics'].items():
            metrics['comparison_windows']=paired[key]['windows']
            metrics['comparison_symbols']=paired[key]['symbols']
            for name in ('direction_accuracy','direction_confusion','mean_absolute_return_error'):
                metrics[name]=paired[key][name]


def compare(dataset: Path, release: Path, config: dict) -> dict:
    """Load one model at a time to respect the shared GPU memory budget."""
    import torch
    from core.kronos_eval import LOOKBACK, _load_predictor
    # Promotion must see the 400-bar context used by confirmation and the evaluator.
    anchors = select_anchors(dataset, symbols_per_market=config['eval_symbols'], lookback=LOOKBACK)
    results = {}
    for label, root in (('base', None), ('candidate', release)):
        predictor = _load_predictor(device='cuda', use_finetuned=root is not None, model_root=root)
        for component in (predictor.model, predictor.tokenizer):
            component.eval()
        with torch.no_grad():
            results[label] = evaluate_model(predictor, dataset, anchors, seed=config['seed'])
        del predictor
        gc.collect()
        torch.cuda.empty_cache()
    pair_comparison(results['base'],results['candidate'])
    results['failures'] = acceptance(results['candidate']['metrics'], results['base']['metrics'],
                                    min_symbols=config['min_eval_symbols'], min_windows=config['min_eval_windows'],
                                    direction_tolerance=config['direction_tolerance'], error_tolerance=config['error_tolerance'],
                                    invalid_policy=config.get('invalid_policy', 'zero'), max_invalid_rate=config.get('max_invalid_rate', .01))
    return results
