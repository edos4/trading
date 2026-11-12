#!/usr/bin/env python3
"""Weekly shared US/PH Kronos training, validation, and transactional replacement."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kronos_weekly import PHT, ensure_vram, exclusive_lock, run_weekly, snapshot_bounds, recover, evaluate_saved
from core.kronos_process import GPU_LOCK_FILE


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run-date', type=date.fromisoformat, help='Intended Saturday (defaults to today in PHT)')
    ap.add_argument('--dry-run', action='store_true', help='Print plan without DB/GPU access or file writes')
    ap.add_argument('--preflight', action='store_true', help='Check CUDA/base weights; no training or mutation')
    ap.add_argument('--rehearsal', action='store_true', help='Full staging run, without activation/deletion/timestamp')
    ap.add_argument('--smoke', action='store_true', help='Tiny staging-only training and evaluation rehearsal')
    ap.add_argument('--recover', action='store_true', help='Recover interrupted activation/cleanup without training')
    ap.add_argument('--evaluate', metavar='RUN_ID', help='Reevaluate a saved complete candidate without retraining')
    ap.add_argument('--resume', metavar='RUN_ID', help='Resume this exact pre-activation training run')
    ap.add_argument('--kronos-home', type=Path, default=Path.home() / 'Kronos')
    ap.add_argument('--epochs', type=int, default=10)
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--exclude-symbol', action='append', default=[], metavar='SYMBOL',
                    help='Exclude a canonical symbol from the entire corpus; repeat for multiple symbols')
    ap.add_argument('--us-weight', type=float, default=.5)
    ap.add_argument('--freshness', type=float, default=.95)
    ap.add_argument('--invalid-policy', choices=('zero','relative'), default=None)
    ap.add_argument('--max-invalid-rate', type=float, default=None)
    ap.add_argument('--min-free-gb', type=float, default=8)
    ap.add_argument('--gpu-wait-minutes', type=float, default=30)
    return ap


def configuration(args) -> dict:
    if args.epochs < 1 or args.batch_size < 1 or not 0 < args.us_weight < 1:
        raise ValueError('Epochs/batch must be positive and both market weights nonzero')
    if args.max_invalid_rate is not None and not 0 <= args.max_invalid_rate <= 1:
        raise ValueError('Invalid forecast rate cap')
    if not 0 < args.freshness <= 1 or args.min_free_gb <= 0 or args.gpu_wait_minutes < 0:
        raise ValueError('Invalid freshness, disk, or GPU wait setting')
    return {'epochs': 1 if args.smoke else args.epochs, 'batch_size': args.batch_size,
            'seed': args.seed, 'lookback': 256, 'predict_len': 10,
            'exclude_symbols': sorted({symbol.strip().upper() for symbol in args.exclude_symbol if symbol.strip()}),
            'train_steps': 2 if args.smoke else 2000, 'val_steps': 2 if args.smoke else 400,
            'market_weights': [args.us_weight, 1 - args.us_weight], 'freshness': args.freshness,
            'min_free_gb': args.min_free_gb, 'eval_symbols': 2 if args.smoke else 50,
            'min_eval_symbols': 1 if args.smoke else 25, 'min_eval_windows': 1 if args.smoke else 50,
            'direction_tolerance': .02, 'error_tolerance': .10, 'smoke': args.smoke,
            'invalid_policy': args.invalid_policy or 'zero', 'max_invalid_rate': .01 if args.max_invalid_rate is None else args.max_invalid_rate}


def _stop(_signum, _frame) -> None:
    raise KeyboardInterrupt


def check_gpu(config: dict) -> dict:
    import torch
    from core.kronos_finetune import TOKENIZER_PATH, MODEL_PATH
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable; weekly training requires a working NVIDIA driver and CUDA torch')
    for root in (TOKENIZER_PATH, MODEL_PATH):
        if not root.is_dir() or not (root / 'config.json').exists():
            raise RuntimeError(f'Missing cached base weights: {root}')
    free, total = torch.cuda.mem_get_info()
    required = ensure_vram(free, config['batch_size'])
    return {'device': torch.cuda.get_device_name(), 'free_vram_bytes': free, 'total_vram_bytes': total,
            'required_vram_bytes': required}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = configuration(args)
        now = datetime.now(PHT)
        intended = args.run_date or now.date()
        if args.recover:
            if args.dry_run or args.preflight or args.resume or args.smoke or args.rehearsal or args.evaluate:
                raise ValueError('--recover cannot be combined with training/inspection modes')
            with exclusive_lock(ROOT / 'logs' / 'kronos-weekly.lock'):
                with exclusive_lock(GPU_LOCK_FILE, timeout=args.gpu_wait_minutes * 60):
                    recover(args.kronos_home.resolve(), ROOT)
            return 0
        if args.evaluate:
            if args.resume or args.smoke:
                raise ValueError('--evaluate cannot be combined with --resume or --smoke')
            if args.dry_run:
                print(json.dumps({'evaluate': args.evaluate, 'rehearsal': args.rehearsal,
                                  'invalid_policy': args.invalid_policy, 'max_invalid_rate': args.max_invalid_rate}))
                return 0
            print(json.dumps({'preflight': check_gpu(config)}), flush=True)
            previous = signal.signal(signal.SIGTERM, _stop)
            try:
                with exclusive_lock(ROOT / 'logs' / 'kronos-weekly.lock'):
                    with exclusive_lock(GPU_LOCK_FILE, timeout=args.gpu_wait_minutes * 60):
                        result = evaluate_saved(args.kronos_home.resolve(), ROOT, args.evaluate,
                                                rehearsal=args.rehearsal, invalid_policy=args.invalid_policy,
                                                max_invalid_rate=args.max_invalid_rate)
                        print(f'Kronos saved evaluation completed: {result}', flush=True)
            finally:
                signal.signal(signal.SIGTERM, previous)
            return 0
        start, end = snapshot_bounds(intended)
        if intended > now.date() and not args.dry_run:
            raise ValueError('Cannot train a future scheduled snapshot')
        plan = {'history_scope': 'all_available', 'window_start': start, 'window_end_exclusive': end.isoformat(),
                'run_date': end.isoformat(), 'rehearsal': args.rehearsal or args.smoke,
                'kronos_home': str(args.kronos_home.resolve()), 'config': config,
                'timestamp_file': str(ROOT / 'kronos-finetuned-at.txt')}
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0
        print(json.dumps({'preflight': check_gpu(config)}), flush=True)
        if args.preflight:
            return 0
        # Production base/model paths intentionally remain those in the existing trainer.
        if args.kronos_home.resolve() != (Path.home() / 'Kronos').resolve():
            raise ValueError('Alternate homes are for dry-run/recovery only; production base paths are fixed')
        previous = signal.signal(signal.SIGTERM, _stop)
        try:
            with exclusive_lock(ROOT / 'logs' / 'kronos-weekly.lock'):
                with exclusive_lock(GPU_LOCK_FILE, timeout=args.gpu_wait_minutes * 60):
                    result = run_weekly(args.kronos_home, ROOT, intended, config,
                                        rehearsal=args.rehearsal or args.smoke, resume_id=args.resume,
                                        gpu_check=check_gpu)
                    print(f'Kronos weekly completed: {result}', flush=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except KeyboardInterrupt:
        print('Kronos weekly interrupted', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Kronos weekly failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
