#!/usr/bin/env python3
"""Isolated GPU worker for Experiment 3; consumes frozen inputs, never later bars."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def file_hashes(folder: Path) -> dict[str, str]:
    """Bind every model file to the saved comparison."""
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[str(path.relative_to(folder))] = digest.hexdigest()
    if not files:
        raise ValueError(f"Empty model directory: {folder}")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    import numpy as np
    import pandas as pd
    import torch
    from core.kronos_eval import MODEL_PATH, TOKENIZER_PATH, load_predictor
    from core.kronos_finetune import FINETUNE_DIR
    from core.kronos_provenance import checkpoint_provenance
    from scripts.kronos_web_forecast import forecast_series

    inputs = json.loads(args.input.read_text())
    ft_root = FINETUNE_DIR.resolve(strict=True)
    models = {"base": {"path": str(MODEL_PATH), "files": file_hashes(MODEL_PATH),
                        "tokenizer_files": file_hashes(TOKENIZER_PATH)},
              "finetuned": {"path": str(ft_root),
                            "provenance": checkpoint_provenance(ft_root, "2026-11-10", required=True)}}
    results = {r["symbol"]: {"symbol": r["symbol"], "days": 2, "lookback": len(r["bars"]),
                           "anchor": {"time": r["bars"][-1]["time"], "value": r["bars"][-1]["close"]},
                           "history": [{"time": p["time"], "value": p["close"]} for p in r["bars"][-120:]]}
               for r in inputs}
    for tag in ("base", "finetuned"):
        predictor, device = load_predictor(use_finetuned=tag == "finetuned",
                                           model_root=ft_root if tag == "finetuned" else None)
        models[tag]["device"] = device
        for stock in inputs:
            # A per-symbol seed makes retries independent of preceding symbols.
            seed = 310_000 + stock["rank"]
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            frame = pd.DataFrame(stock["bars"])
            frame.index = pd.to_datetime(frame["time"])
            results[stock["symbol"]][tag] = forecast_series(predictor, frame, 2, 400, 1)
            print(f"Experiment 3 | {tag} {stock['rank']}/50 {stock['symbol']}", flush=True)
        del predictor
        if device == "cuda":
            torch.cuda.empty_cache()
    args.out.write_text(json.dumps({"results": list(results.values()), "models": models,
                                    "settings": {"max_lookback": 400, "min_context": 60,
                                                 "sample_count": 1, "temperature": 1.0,
                                                 "top_p": .9, "seed": "310000 + rank per model"}}, allow_nan=False))
    return 0


if __name__ == "__main__":
    from core.kronos_process import locked_main
    raise SystemExit(locked_main(main))
