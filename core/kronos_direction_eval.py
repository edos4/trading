"""Exploratory paired comparisons with existing, dated Kronos return forecasts."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from core.kronos_confirm import classify
from learn.kronos_direction_dataset import LABELS, DirectionCorpus

if TYPE_CHECKING:
    from core.kronos_direction import DirectionModel


def compare_saved_kronos(corpus: DirectionCorpus, model: DirectionModel, path: Path,
                         lower: date, upper: date) -> dict:
    """Use identical eligible contexts; never treat reused forecasts as final tests."""
    from core.kronos_direction import classification_metrics
    saved = json.loads(Path(path).read_text())
    predictions = {}
    for label in ('base', 'candidate'):
        predictions[label] = {}
        for row in saved[label]['rows']:
            if not row['valid'] or row.get('abstained') or row['horizon'] != model.horizon:
                continue
            if 'label_anchor_date' not in row or 'label_anchor_close' not in row:
                raise ValueError('Saved Kronos comparisons require dated, unrounded recalculated rows')
            key = row['market'], row['symbol'], row['label_anchor_date']
            if key in predictions[label]:
                raise ValueError('Duplicate saved Kronos context')
            predictions[label][key] = row
    symbols = {(s['market'], s['name']): index for index, s in enumerate(corpus.symbols)}
    ranges = {}
    for start, count, index in corpus.ranges:
        ranges.setdefault(index, []).append((start, count))
    targets, pairs = [], []
    for key in sorted(predictions['base'].keys() & predictions['candidate'].keys()):
        market, name, day = key
        index = symbols.get((market, name))
        if index is None:
            continue
        symbol = corpus.symbols[index]
        offset, count = symbol['offset'], symbol['count']
        days = corpus.days[offset:offset + count]
        anchor = int(np.searchsorted(days, date.fromisoformat(day).toordinal()))
        target = offset + anchor + 1
        if (anchor >= count or int(days[anchor]) != date.fromisoformat(day).toordinal()
                or target + 2 >= offset + count or anchor + 1 < corpus.lookback):
            continue
        if not (int(corpus.days[target]) >= lower.toordinal()
                and int(corpus.days[target + 2]) < upper.toordinal()):
            continue
        entries = ranges.get(index, [])
        position = int(np.searchsorted([start for start, _ in entries], target, side='right')) - 1
        if position < 0 or target >= entries[position][0] + entries[position][1]:
            continue
        anchor_close = float(corpus.features[target - 1, 3])
        for label in ('base', 'candidate'):
            if not np.isclose(predictions[label][key]['label_anchor_close'], anchor_close, rtol=1e-7, atol=0):
                raise ValueError('Saved Kronos context price differs from the experiment source')
        targets.append((target, index))
        pairs.append(key)
    result = {'test_role': 'reused_development_comparison', 'groups': {},
              'note': 'These saved forecasts and their periods already influenced earlier work; not an untouched final test.'}
    if not targets:
        result['matched_cases'] = 0
        return result
    examples = corpus.examples(targets)
    column = 0 if model.horizon == 1 else 1
    candidate = model.predict(examples['x'])
    for market in ('us', 'ph'):
        mask = examples['markets'] == market
        if not mask.any():
            continue
        metrics = {'direction_classifier': classification_metrics(examples['y'][mask, column], candidate[mask])}
        for label in ('base', 'candidate'):
            predicted = np.asarray([LABELS.index(classify(predictions[label][key]['forecast_return'], band * 100))
                                    for key, band in zip(pairs, examples['bands'])])
            metrics[f'kronos_{label}'] = classification_metrics(examples['y'][mask, column], predicted[mask])
        result['groups'][market] = metrics
    result['matched_cases'] = len(targets)
    return result
