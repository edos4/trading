#!/usr/bin/env python3
"""Train staged up/down/flat classifiers or evaluate their reserved final period."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kronos_direction import DirectionConfig, evaluate_final, run_direction_experiment
from learn.kronos_direction_dataset import DirectionCorpus, QualityPolicy


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dataset', type=Path, required=True, help='Existing frozen weekly corpus; opened read-only')
    ap.add_argument('--output', type=Path, required=True, help='New experiment directory, or locked selection for final evaluation')
    ap.add_argument('--final-test-start', type=date.fromisoformat, help='First reserved target date, excluded from development')
    ap.add_argument('--evaluate-final', action='store_true', help='Consume the complete reserved period once, with the locked policy')
    ap.add_argument('--kronos-evaluation', type=Path, help='Optional dated recalculated Kronos forecasts for matched development comparisons')
    ap.add_argument('--dry-run', action='store_true', help='Print configuration without reading data or writing files')
    ap.add_argument('--lookback', type=int, default=400)
    ap.add_argument('--folds', type=int, default=3)
    ap.add_argument('--validation-weeks', type=int, default=8)
    ap.add_argument('--test-weeks', type=int, default=4)
    ap.add_argument('--final-test-days', type=int, default=28)
    ap.add_argument('--gap-sessions', type=int, default=3)
    ap.add_argument('--train-samples', type=int, default=32_000)
    ap.add_argument('--validation-samples', type=int, default=6_000)
    ap.add_argument('--test-samples', type=int, default=6_000)
    ap.add_argument('--min-eval-samples', type=int, default=50)
    ap.add_argument('--min-eval-symbols', type=int, default=10)
    ap.add_argument('--min-class-samples', type=int, default=5)
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch-size', type=int, default=512)
    ap.add_argument('--learning-rate', type=float, default=.01)
    ap.add_argument('--l2', type=float, default=.001)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--half-life-days', type=float, default=730.)
    ap.add_argument('--uniform-share', type=float, default=.1)
    ap.add_argument('--class-weight', choices=('none', 'balanced'), default='balanced')
    ap.add_argument('--selection-metric', choices=('macro_f1', 'balanced_accuracy'), default='macro_f1')
    ap.add_argument('--exclude-symbol', action='append', default=[], help='Additional canonical exclusion; AXRX/GASE/GBDMF remain excluded')
    ap.add_argument('--us-min-turnover', type=float, default=QualityPolicy().us_min_turnover)
    ap.add_argument('--ph-min-turnover', type=float, default=QualityPolicy().ph_min_turnover)
    ap.add_argument('--max-zero-volume-fraction', type=float, default=.5)
    ap.add_argument('--max-unchanged-fraction', type=float, default=.8)
    ap.add_argument('--max-day-price-ratio', type=float, default=4.)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.evaluate_final:
            if args.final_test_start:
                raise ValueError('Final-test dates come from the locked selection')
            selection = json.loads((args.output / 'selection.json').read_text())
            quality = QualityPolicy(**selection['quality'])
            lookback, excluded = selection['lookback'], tuple(selection['excluded_symbols'])
            config = DirectionConfig(**selection['config'])
        else:
            if args.final_test_start is None:
                raise ValueError('--final-test-start is required before training')
            config = DirectionConfig(**{name: getattr(args, name) for name in DirectionConfig.__dataclass_fields__})
            quality = QualityPolicy(us_min_turnover=args.us_min_turnover, ph_min_turnover=args.ph_min_turnover,
                                    max_zero_volume_fraction=args.max_zero_volume_fraction,
                                    max_unchanged_fraction=args.max_unchanged_fraction,
                                    max_day_price_ratio=args.max_day_price_ratio)
            lookback = args.lookback
            excluded = tuple(sorted({'AXRX', 'GASE', 'GBDMF'} |
                                    {name.strip().upper() for name in args.exclude_symbol if name.strip()}))
        if lookback < max(61, quality.window):
            raise ValueError('Lookback is too short for the feature/quality contract')
        if args.dry_run:
            print(json.dumps({'dataset': str(args.dataset), 'output': str(args.output),
                              'config': asdict(config), 'quality': asdict(quality), 'excluded_symbols': excluded,
                              'lookback': lookback, 'final_test_start': str(args.final_test_start),
                              'evaluate_final': args.evaluate_final}, indent=2))
            return 0
        corpus = DirectionCorpus(args.dataset, lookback=lookback, quality=quality, excluded_symbols=excluded)
        if args.evaluate_final:
            if 'flat_policy' in selection:
                from core.arrow_flat import evaluate_revision
                result = evaluate_revision(corpus, args.output)
            else:
                result = evaluate_final(corpus, args.output)
            print(json.dumps(result, indent=2))
        else:
            result = run_direction_experiment(corpus, args.output, args.final_test_start, config,
                                              kronos_evaluation=args.kronos_evaluation)
            print(json.dumps({'status': 'trained_final_pending', 'output': str(args.output.resolve()),
                              'chosen_sampling': result['chosen_sampling'], 'elapsed_seconds': result['elapsed_seconds'],
                              'reserved_final_period': [result['final_test_start'], result['final_test_end']]}, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f'Direction experiment failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
