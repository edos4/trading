#!/usr/bin/env python3
"""Name, inspect, and run Arrow's saved daily direction classifiers."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.arrow import Arrow, DEFAULT_ARROW, register_arrow


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest='command', required=True)
    register = commands.add_parser('register', help='Name an existing classifier pair Arrow')
    register.add_argument('--model-dir', type=Path, required=True)
    register.add_argument('--alias', type=Path, default=DEFAULT_ARROW)
    info = commands.add_parser('info', help='Show Arrow model and validation status')
    info.add_argument('--model-dir', type=Path, default=DEFAULT_ARROW)
    predict = commands.add_parser('predict', help='Predict up/down/flat from completed daily bars')
    predict.add_argument('--model-dir', type=Path, default=DEFAULT_ARROW)
    predict.add_argument('--symbol', required=True)
    predict.add_argument('--market', choices=('us', 'ph'), default='us')
    predict.add_argument('--horizon', type=int, choices=(1, 3), default=1)
    predict.add_argument('--as-of', type=date.fromisoformat)
    predict.add_argument('--csv', type=Path, help='Read date/open/high/low/close/volume CSV instead of PostgreSQL')
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == 'register':
            result = register_arrow(args.model_dir, args.alias)
        else:
            arrow = Arrow.load(args.model_dir)
            if args.command == 'info':
                result = arrow.info()
            else:
                if args.csv:
                    import pandas as pd
                    frame = pd.read_csv(args.csv)
                    if 'date' not in frame:
                        raise ValueError('CSV must contain a date column')
                    frame.index = pd.to_datetime(frame.pop('date'), errors='raise')
                else:
                    from core.market import last_closed_session_date
                    from scripts.kronos_confirm_worker import load_symbol_df
                    cutoff = args.as_of or last_closed_session_date(args.market)
                    frame = load_symbol_df(args.symbol, as_of=cutoff.isoformat(), market=args.market)
                result = arrow.predict(frame, args.symbol, args.market, args.horizon, as_of=args.as_of)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        print(f'Arrow failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
