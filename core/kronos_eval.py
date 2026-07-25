"""
core/kronos_eval.py — orchestrates `python main.py --kronos-test`: run
Kronos-base (https://github.com/shiyu-coder/Kronos) walk-forward over the
historical daily CSVs in /home/r00t/stocks_data and score its +1 day /
+1 week close-price forecasts against what actually happened.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from learn.dataset import DEFAULT_DATA_DIR, iter_ticker_frames
from utils.logger import log

KRONOS_REPO_DIR = Path.home() / "Kronos"
if str(KRONOS_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(KRONOS_REPO_DIR))

TOKENIZER_PATH = KRONOS_REPO_DIR / "weights" / "Kronos-Tokenizer-base"
MODEL_PATH = KRONOS_REPO_DIR / "weights" / "Kronos-base"
MAX_CONTEXT = 512

DAY_AHEAD = 1
WEEK_AHEAD = 5  # trading days


@dataclass
class WindowResult:
    symbol: str
    asof: pd.Timestamp
    actual_1d: float
    pred_1d: float
    actual_1w: float
    pred_1w: float


def _load_predictor(device: str = "cpu", use_finetuned: bool = False):
    from model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer_path, model_path = TOKENIZER_PATH, MODEL_PATH
    if use_finetuned:
        from core.kronos_finetune import PREDICTOR_OUT, TOKENIZER_OUT

        if TOKENIZER_OUT.exists() and PREDICTOR_OUT.exists():
            tokenizer_path, model_path = TOKENIZER_OUT, PREDICTOR_OUT
        else:
            log.warning(
                f"Kronos | use_finetuned=True but no checkpoint at {PREDICTOR_OUT} — "
                "falling back to base weights. Run `python main.py --kronos-finetune` first."
            )

    log.info(f"Kronos | loading {model_path} on {device} ...")
    tokenizer = KronosTokenizer.from_pretrained(str(tokenizer_path))
    model = Kronos.from_pretrained(str(model_path))
    return KronosPredictor(model, tokenizer, device=device, max_context=MAX_CONTEXT)


def _eval_windows(
    df: pd.DataFrame, lookback: int, windows: int, stride: int, start_date: pd.Timestamp | None = None
) -> list[tuple[int, int, int]]:
    """Return (context_start, context_end, future_end) index triples, most recent first.

    Windows walk backward from the end of df, so start_date doesn't need to trim
    df itself (older bars are still needed as lookback context) — it just stops
    the walk once the window's asof (context_end) date would predate it.
    """
    pred_len = WEEK_AHEAD
    spans = []
    end = len(df)
    for _ in range(windows):
        context_end = end - pred_len
        context_start = context_end - lookback
        if context_start < 0:
            break
        if start_date is not None and df.index[context_end - 1] < start_date:
            break
        spans.append((context_start, context_end, end))
        end -= stride
    return spans


def _run_symbol(
    predictor, symbol: str, df: pd.DataFrame, lookback: int, windows: int, stride: int,
    sample_count: int = 1, start_date: pd.Timestamp | None = None,
) -> list[WindowResult]:
    results = []
    for context_start, context_end, future_end in _eval_windows(df, lookback, windows, stride, start_date):
        x_df = df.iloc[context_start:context_end][["open", "high", "low", "close", "volume"]]
        y_actual = df.iloc[context_end:future_end]
        if len(y_actual) < WEEK_AHEAD:
            continue
        x_timestamp = pd.Series(x_df.index)
        y_timestamp = pd.Series(y_actual.index)

        pred_df = predictor.predict(
            df=x_df.reset_index(drop=True),
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=WEEK_AHEAD,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )

        last_close = float(x_df["close"].iloc[-1])
        actual_1d = float(y_actual["close"].iloc[DAY_AHEAD - 1]) / last_close - 1.0
        actual_1w = float(y_actual["close"].iloc[WEEK_AHEAD - 1]) / last_close - 1.0
        pred_1d = float(pred_df["close"].iloc[DAY_AHEAD - 1]) / last_close - 1.0
        pred_1w = float(pred_df["close"].iloc[WEEK_AHEAD - 1]) / last_close - 1.0

        results.append(
            WindowResult(
                symbol=symbol,
                asof=x_df.index[-1],
                actual_1d=actual_1d,
                pred_1d=pred_1d,
                actual_1w=actual_1w,
                pred_1w=pred_1w,
            )
        )
    return results


def _score(results: list[WindowResult], actual_attr: str, pred_attr: str) -> dict:
    actual = np.array([getattr(r, actual_attr) for r in results])
    pred = np.array([getattr(r, pred_attr) for r in results])
    if len(actual) == 0:
        return {"n": 0}
    mae = float(np.mean(np.abs(pred - actual)))
    naive_mae = float(np.mean(np.abs(actual)))  # baseline: predict no change
    direction_hits = np.sign(pred) == np.sign(actual)
    return {
        "n": len(actual),
        "mae_pct": mae * 100,
        "naive_mae_pct": naive_mae * 100,
        "directional_accuracy_pct": float(np.mean(direction_hits)) * 100,
    }


def run_kronos_test(
    data_dir: Path = DEFAULT_DATA_DIR,
    n_symbols: int = 20,
    windows_per_symbol: int = 3,
    lookback: int = 400,
    stride: int = 20,
    min_bars: int = 500,
    seed: int = 42,
    device: str | None = None,
    liquid_only: bool = False,
    liquidity_window: int = 60,
    sample_count: int = 1,
    start_date: str | None = "2026-03-01",
    use_finetuned: bool = False,
) -> None:
    if device is None:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info(
        f"Kronos-test | data_dir={data_dir} symbols={n_symbols} device={device} "
        f"windows/symbol={windows_per_symbol} lookback={lookback} liquid_only={liquid_only} "
        f"sample_count={sample_count} start_date={start_date} use_finetuned={use_finetuned}"
    )
    start_ts = pd.Timestamp(start_date) if start_date else None

    candidates = [
        (symbol, df) for symbol, df in iter_ticker_frames(data_dir) if len(df) >= min_bars
    ]
    if not candidates:
        log.error("Kronos-test | no tickers with enough history found — check data_dir")
        return

    if start_ts is not None:
        # Tickers whose data ends before start_date are stale/delisted — their
        # only available windows would be scored against old market regimes
        # instead of the current one, which is what start_date is for.
        n_before = len(candidates)
        candidates = [(s, df) for s, df in candidates if df.index[-1] >= start_ts]
        if len(candidates) < n_before:
            log.warning(
                f"Kronos-test | dropped {n_before - len(candidates)} tickers with no data on/after {start_date}"
            )
        if not candidates:
            log.error(f"Kronos-test | no tickers have data on/after {start_date} — check data_dir")
            return
    pool_size = len(candidates)

    if liquid_only:
        # Rank by recent avg dollar volume (close * volume) — picks liquid
        # large/mid-caps instead of the random-sample junk that dominates
        # this CSV set by ticker count (penny/OTC names).
        def dollar_volume(df: pd.DataFrame) -> float:
            tail = df.tail(liquidity_window)
            return float((tail["close"] * tail["volume"]).mean())

        ranked = [(dollar_volume(df), symbol, df) for symbol, df in candidates]
        # NaN dollar-volume (e.g. a stale/all-NaN recent window) breaks
        # list.sort()'s ordering guarantees since NaN comparisons are always
        # False — one NaN entry can silently scramble the ranking of every
        # ticker around it, not just its own position. Drop those instead.
        n_before = len(ranked)
        ranked = [r for r in ranked if not math.isnan(r[0])]
        if len(ranked) < n_before:
            log.warning(f"Kronos-test | dropped {n_before - len(ranked)} tickers with NaN dollar-volume before ranking")
        ranked.sort(key=lambda r: r[0], reverse=True)
        candidates = [(symbol, df) for _dv, symbol, df in ranked[:n_symbols]]
    else:
        rng = random.Random(seed)
        rng.shuffle(candidates)
        candidates = candidates[:n_symbols]
    log.info(f"Kronos-test | evaluating {len(candidates)} symbols (of {pool_size} with >= {min_bars} bars)")

    predictor = _load_predictor(device=device, use_finetuned=use_finetuned)

    all_results: list[WindowResult] = []
    for symbol, df in candidates:
        try:
            results = _run_symbol(
                predictor, symbol, df, lookback, windows_per_symbol, stride, sample_count, start_ts
            )
        except Exception as exc:
            log.warning(f"Kronos-test | {symbol}: skipped ({exc})")
            continue
        all_results.extend(results)
        log.info(f"Kronos-test | {symbol}: {len(results)} windows")

    if not all_results:
        log.error("Kronos-test | no windows evaluated — nothing to score")
        return

    score_1d = _score(all_results, "actual_1d", "pred_1d")
    score_1w = _score(all_results, "actual_1w", "pred_1w")

    print()
    print("=" * 60)
    print(f"  Kronos-base accuracy — {len(candidates)} symbols, {len(all_results)} windows")
    print("=" * 60)
    for label, s in (("+1 day", score_1d), ("+1 week", score_1w)):
        print(f"  {label:8s}  n={s['n']:<5d} "
              f"MAE={s['mae_pct']:.2f}%  (naive MAE={s['naive_mae_pct']:.2f}%)  "
              f"direction hit={s['directional_accuracy_pct']:.1f}%")
    print("=" * 60)
    print("MAE = mean absolute error. Average of |predicted % move − actual % move| over all windows. Lower = better.")
    print("Naive MAE = same metric but 'prediction' is 0% change (tomorrow = today, no move). Baseline for 'model add no value' case.")
    print()
