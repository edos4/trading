#!/usr/bin/env python3
"""Build a new Arrow Flat decision policy using only pre-feedback validation."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    from core.arrow import Arrow, DEFAULT_ARROW, register_arrow
    from core.arrow_flat import build_revision
    from learn.kronos_direction_dataset import DirectionCorpus, QualityPolicy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--parent-model', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--forward-start', required=True, type=date.fromisoformat)
    parser.add_argument('--feedback-through', required=True, type=date.fromisoformat)
    parser.add_argument('--activate', action='store_true', help='Use the validated revision for future Arrow predictions')
    parser.add_argument('--alias', type=Path, default=DEFAULT_ARROW)
    args = parser.parse_args(argv)
    try:
        parent = Arrow.load(args.parent_model)
        corpus = DirectionCorpus(args.dataset, lookback=parent.selection['lookback'],
                                 quality=QualityPolicy(**parent.selection['quality']),
                                 excluded_symbols=tuple(parent.selection['excluded_symbols']))
        selection = build_revision(corpus, args.parent_model, args.output, args.forward_start, args.feedback_through)
        register_arrow(args.output, args.alias if args.activate else args.output)
        print(json.dumps({'model': Arrow.load(args.output).info(), 'policy': selection['flat_policy'],
                          'activated_for_future_predictions': args.activate,
                          'validation_report': str(args.output / 'flat-validation.json')}, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f'Arrow Flat revision failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
