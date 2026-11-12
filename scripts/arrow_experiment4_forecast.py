#!/usr/bin/env python3
"""CPU Arrow forecasts from Experiment 4's frozen, already dated input bars."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def generate(inputs: list[dict], plan: dict, model_dir: Path) -> dict:
    import pandas as pd
    from core.arrow import Arrow
    from core.kronos_confirm import atr_pct_from_frame, sideways_band_pct
    arrow = Arrow.load(model_dir)
    results = []
    for item in inputs:
        frame = pd.DataFrame(item['bars'])
        frame.index = pd.to_datetime(frame['time'])
        anchor = {'time': plan['as_of'], 'value': float(frame.close.iloc[-1])}
        band = sideways_band_pct(atr_pct_from_frame(frame, anchor['value'])) / 100 if len(frame) >= 15 else None
        predictions = []
        for horizon in (1, 3):
            try:
                result = arrow.predict(frame, item['symbol'], 'us', horizon, as_of=date.fromisoformat(plan['as_of']))
            except ValueError as exc:
                result = {'direction': None, 'abstained': True, 'reasons': [str(exc)]}
            predictions.append({'horizon': horizon, 'date': plan['target_dates'][str(horizon)],
                                'direction': result['direction'], 'abstained': result['abstained'],
                                'reasons': result.get('reasons', []), 'probabilities': result.get('probabilities'),
                                'flat_band_return': result.get('flat_band_return', band)})
        results.append({'symbol': item['symbol'], 'anchor': anchor, 'lookback': len(frame),
                        'history': [{'time': row['time'], 'value': row['close']} for row in item['bars'][-120:]],
                        'forecasts': predictions})
    return {'results': results, 'model': {**arrow.info(), 'selection_sha256': arrow.card['selection_sha256'],
                                        'checkpoints': arrow.selection['models']},
            'settings': {'horizons_sessions': [1, 3], 'lookback': arrow.selection['lookback'],
                         'flat_rule': 'frozen ATR band shared by actual and predicted directions', 'device': 'cpu'}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(args.input.read_text())
    payload = generate(frozen['inputs'], frozen['schedule'], args.model_dir)
    args.out.write_text(json.dumps(payload, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
