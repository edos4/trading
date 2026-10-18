"""
scripts/kronos_finetune.py — fine-tune Kronos-base on stocks_history.

Runs the campaign from docs/kronos-finetune.md: lean dataset build →
tokenizer + predictor fine-tune. All checkpoints/datasets live under
~/Kronos (never in the repo, never deployed).

    .venv-kronos/bin/python scripts/kronos_finetune.py --all-symbols --epochs 10
    .venv-kronos/bin/python scripts/kronos_finetune.py --all-symbols --epochs 10 --smoke-steps 100
    systemd-run --user --unit=kronos-finetune --collect \\
      .venv-kronos/bin/python scripts/kronos_finetune.py --all-symbols --epochs 10

Must run inside .venv-kronos (torch cu128+), not the app .venv.
`main.py` is intentionally untouched — this CLI is the only entrypoint.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.kronos_finetune import DATASET_DIR, run_kronos_finetune  # noqa: E402
from learn.kronos_dataset import DEFAULT_TRAIN_CUTOFF  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-symbols", action=argparse.BooleanOptionalAction, default=True,
                    help="use the lean full-universe corpus (default: on)")
    ap.add_argument("--train-cutoff", default=DEFAULT_TRAIN_CUTOFF,
                    help="bars at/after this date are held out for evaluation "
                         "(default: %(default)s)")
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--max-stale-days", type=int, default=None,
                    help="drop symbols whose last bar is older than this "
                         "(default: keep delisted names)")
    ap.add_argument("--dataset-dir", default=str(DATASET_DIR))
    ap.add_argument("--epochs", type=int, default=10,
                    help="epochs for BOTH stages (overrides the two below)")
    ap.add_argument("--tokenizer-epochs", type=int, default=None)
    ap.add_argument("--predictor-epochs", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=16,
                    help="16 sized for co-tenancy with FreeToken on 16 GB")
    ap.add_argument("--lookback", type=int, default=256)
    ap.add_argument("--predict-len", type=int, default=10)
    ap.add_argument("--clip", type=float, default=5.0)
    ap.add_argument("--tokenizer-lr", type=float, default=2e-4)
    ap.add_argument("--predictor-lr", type=float, default=4e-5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-tokenizer", action="store_true",
                    help="skip stage 1 (only if a fine-tuned tokenizer exists)")
    ap.add_argument("--base-tokenizer", action="store_true",
                    help="with --skip-tokenizer: train predictor on BASE tokenizer "
                         "encodings instead of the fine-tuned one (isolation test)")
    ap.add_argument("--smoke-steps", type=int, default=0,
                    help="measure per-step time over N steps first and print "
                         "the projected epoch/total wall clock")
    ap.add_argument("--resume", action="store_true",
                    help="restart from per-epoch last.pt (model+optimizer+scheduler), "
                         "not base weights; same --epochs as the killed run")
    ap.add_argument("--force-rebuild", action="store_true",
                    help="rebuild the dataset even if cached")
    args = ap.parse_args(argv)

    try:
        import torch  # noqa: F401
    except ImportError:
        print(
            "kronos-finetune | torch is not installed here — run inside .venv-kronos:\n"
            "  python3 -m venv .venv-kronos\n"
            "  .venv-kronos/bin/pip install -r requirements-kronos.txt "
            "--index-url https://download.pytorch.org/whl/cu128"
        )
        return 2

    tok_epochs = args.tokenizer_epochs if args.tokenizer_epochs is not None else args.epochs
    pred_epochs = args.predictor_epochs if args.predictor_epochs is not None else args.epochs

    run_kronos_finetune(
        min_bars=args.min_bars,
        lookback=args.lookback,
        predict_len=args.predict_len,
        clip=args.clip,
        tokenizer_epochs=tok_epochs,
        predictor_epochs=pred_epochs,
        batch_size=args.batch_size,
        tokenizer_lr=args.tokenizer_lr,
        predictor_lr=args.predictor_lr,
        seed=args.seed,
        skip_tokenizer=args.skip_tokenizer,
        base_tokenizer=args.base_tokenizer,
        all_symbols=args.all_symbols,
        train_cutoff=args.train_cutoff,
        max_stale_days=args.max_stale_days,
        dataset_dir=args.dataset_dir,
        smoke_steps=args.smoke_steps,
        resume=args.resume,
        force_rebuild=args.force_rebuild,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
