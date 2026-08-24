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
# Official examples (prediction_example.py, prediction_cn_markets_day.py) use 400.
LOOKBACK = 400

DAY_AHEAD = 1
WEEK_AHEAD = 3  # trading days — gate horizon (3d veto), not the demo pred_len=120


def with_amount(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → OHLCV+amount using KronosPredictor's documented fallback.

    When a feed has no real dollar-turnover column, the official predictor
    fills ``amount = volume * mean(OHLC)``. We materialize that here so gate,
    eval, and finetune all feed identical feature semantics.
    """
    out = df[["open", "high", "low", "close", "volume"]].copy()
    out["amount"] = out["volume"] * out[["open", "high", "low", "close"]].mean(axis=1)
    return out


def predict_1w_return(
    predictor,
    df: pd.DataFrame,
    *,
    sample_count: int = 1,
    lookback: int = LOOKBACK,
) -> tuple[float, float] | None:
    """Run Kronos on the last ``lookback`` bars; return (pred_1w, last_close).

    ``pred_1w`` is close-to-close % move over ``WEEK_AHEAD`` trading days.
    Returns None if history is too short or predict fails.
    """
    if df is None or len(df) < max(60, min(lookback, len(df))):
        return None
    use = min(lookback, len(df), MAX_CONTEXT)
    if use < 60:
        return None
    try:
        x_df = with_amount(df.iloc[-use:])
        last_close = float(x_df["close"].iloc[-1])
        if last_close <= 0:
            return None
        x_timestamp = pd.Series(x_df.index)
        y_timestamp = pd.Series(
            pd.bdate_range(
                start=x_df.index[-1] + pd.Timedelta(days=1),
                periods=WEEK_AHEAD,
            )
        )
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
        pred_1w = float(pred_df["close"].iloc[WEEK_AHEAD - 1]) / last_close - 1.0
        return pred_1w, last_close
    except Exception:
        log.exception("Kronos | predict_1w_return failed")
        return None


@dataclass
class WindowResult:
    symbol: str
    asof: pd.Timestamp
    actual_1d: float
    pred_1d: float
    actual_1w: float
    pred_1w: float
    # Prior WEEK_AHEAD close-to-close return ending at asof (persistence baseline).
    persist_1w: float = float("nan")


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
        x_df = with_amount(df.iloc[context_start:context_end])
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

        asof_i = context_end - 1
        prior_i = asof_i - WEEK_AHEAD
        if prior_i >= 0:
            prior_close = float(df["close"].iloc[prior_i])
            persist_1w = (
                last_close / prior_close - 1.0 if prior_close > 0 else float("nan")
            )
        else:
            persist_1w = float("nan")

        results.append(
            WindowResult(
                symbol=symbol,
                asof=x_df.index[-1],
                actual_1d=actual_1d,
                pred_1d=pred_1d,
                actual_1w=actual_1w,
                pred_1w=pred_1w,
                persist_1w=persist_1w,
            )
        )
    return results


def _score(results: list[WindowResult], actual_attr: str, pred_attr: str) -> dict:
    actual = np.array([getattr(r, actual_attr) for r in results], dtype=float)
    pred = np.array([getattr(r, pred_attr) for r in results], dtype=float)
    if len(actual) == 0:
        return {"n": 0}
    mae = float(np.mean(np.abs(pred - actual)))
    naive_mae = float(np.mean(np.abs(actual)))  # baseline: predict no change
    direction_hits = np.sign(pred) == np.sign(actual)
    # Mean 1w return if always trading the predicted sign (unit notional).
    signed_ret = float(np.mean(np.sign(pred) * actual))
    return {
        "n": len(actual),
        "mae_pct": mae * 100,
        "naive_mae_pct": naive_mae * 100,
        "directional_accuracy_pct": float(np.mean(direction_hits)) * 100,
        "signed_return_pct": signed_ret * 100,
    }


def _score_persistence(results: list[WindowResult]) -> dict:
    """Same metrics as `_score`, but prediction = prior-week return (persist_1w)."""
    usable = [r for r in results if not math.isnan(r.persist_1w)]
    return _score(usable, "actual_1w", "persist_1w")


def score_gate_rule(
    results: list[WindowResult],
    min_move: float,
) -> dict:
    """Metrics matching `kronos_gate`: only windows with |pred_1w| >= min_move.

    This is the project-relevant slice: live gate vetoes weak forecasts and only
    confirms when magnitude clears `settings.kronos_min_move_pct`. Coverage is
    the fraction of calendar windows that would clear the magnitude floor (as if
    every asof were a pattern signal — still unconditional on patterns).
    """
    n_all = len(results)
    if n_all == 0:
        return {"n": 0, "n_all": 0, "coverage_pct": 0.0, "min_move": min_move}

    passed = [r for r in results if abs(r.pred_1w) >= min_move]
    base = _score(passed, "actual_1w", "pred_1w")
    if base["n"] == 0:
        return {
            "n": 0,
            "n_all": n_all,
            "coverage_pct": 0.0,
            "min_move": min_move,
            "mae_pct": float("nan"),
            "naive_mae_pct": float("nan"),
            "directional_accuracy_pct": float("nan"),
            "signed_return_pct": float("nan"),
        }

    pred = np.array([r.pred_1w for r in passed])
    actual = np.array([r.actual_1w for r in passed])
    buy_mask = pred > 0
    sell_mask = pred < 0
    buy_hit = (
        float(np.mean(actual[buy_mask] > 0)) * 100 if buy_mask.any() else float("nan")
    )
    sell_hit = (
        float(np.mean(actual[sell_mask] < 0)) * 100 if sell_mask.any() else float("nan")
    )
    return {
        **base,
        "n_all": n_all,
        "coverage_pct": 100.0 * len(passed) / n_all,
        "min_move": min_move,
        "n_buy": int(buy_mask.sum()),
        "n_sell": int(sell_mask.sum()),
        "buy_dir_hit_pct": buy_hit,
        "sell_dir_hit_pct": sell_hit,
    }


def bootstrap_ci(
    results: list[WindowResult],
    *,
    actual_attr: str = "actual_1w",
    pred_attr: str = "pred_1w",
    n_boot: int = 1000,
    seed: int = 42,
    min_move: float | None = None,
) -> dict:
    """Percentile bootstrap CIs for MAE% and directional accuracy%.

    When ``min_move`` is set, each resample re-applies the gate magnitude filter
    (coverage varies per draw).
    """
    empty = {
        "n": 0,
        "mae_pct_ci": (float("nan"), float("nan"), float("nan")),
        "dir_acc_pct_ci": (float("nan"), float("nan"), float("nan")),
    }
    if not results:
        return empty

    rng = np.random.default_rng(seed)
    mae_s: list[float] = []
    dir_s: list[float] = []
    n = len(results)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sample = [results[i] for i in idx]
        if min_move is not None:
            sample = [r for r in sample if abs(getattr(r, pred_attr)) >= min_move]
        if len(sample) < 2:
            continue
        s = _score(sample, actual_attr, pred_attr)
        mae_s.append(s["mae_pct"])
        dir_s.append(s["directional_accuracy_pct"])

    if not mae_s:
        return empty

    def _pct(xs: list[float]) -> tuple[float, float, float]:
        arr = np.asarray(xs, dtype=float)
        lo, mid, hi = np.percentile(arr, [2.5, 50.0, 97.5])
        return float(lo), float(mid), float(hi)

    return {
        "n": n,
        "n_boot_kept": len(mae_s),
        "mae_pct_ci": _pct(mae_s),
        "dir_acc_pct_ci": _pct(dir_s),
    }


def majority_sign_baseline(results: list[WindowResult]) -> dict:
    """Always predict the sample's majority actual sign (in-sample oracle bias check).

    Not a tradeable baseline — shows how much dir_acc a constant-sign guess gets
    on a one-sided tape (important when Kronos is systematically bearish/bullish).
    """
    if not results:
        return {"n": 0}
    actual = np.array([r.actual_1w for r in results])
    maj = 1.0 if np.mean(actual > 0) >= 0.5 else -1.0
    pred = np.full_like(actual, maj)
    mae = float(np.mean(np.abs(pred - actual)))
    naive_mae = float(np.mean(np.abs(actual)))
    dir_hits = np.sign(pred) == np.sign(actual)
    return {
        "n": len(actual),
        "majority_sign": int(maj),
        "mae_pct": mae * 100,
        "naive_mae_pct": naive_mae * 100,
        "directional_accuracy_pct": float(np.mean(dir_hits)) * 100,
        "signed_return_pct": float(np.mean(np.sign(pred) * actual)) * 100,
    }


def run_kronos_test(
    data_dir: Path = DEFAULT_DATA_DIR,
    n_symbols: int = 20,
    windows_per_symbol: int = 3,
    lookback: int = LOOKBACK,
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
    try:
        from config import settings

        min_move = settings.kronos_min_move_pct
    except Exception:
        min_move = 0.06
    gate = score_gate_rule(all_results, min_move)
    persist = _score_persistence(all_results)

    print()
    print("=" * 60)
    print(f"  Kronos-base accuracy — {len(candidates)} symbols, {len(all_results)} windows")
    print("=" * 60)
    for label, s in (("+1 day", score_1d), ("+1 week", score_1w)):
        print(f"  {label:8s}  n={s['n']:<5d} "
              f"MAE={s['mae_pct']:.2f}%  (naive MAE={s['naive_mae_pct']:.2f}%)  "
              f"direction hit={s['directional_accuracy_pct']:.1f}%")
    if persist.get("n"):
        print(
            f"  persist  n={persist['n']:<5d} "
            f"MAE={persist['mae_pct']:.2f}%  "
            f"direction hit={persist['directional_accuracy_pct']:.1f}%  "
            f"(prior-week return baseline)"
        )
    if gate.get("n"):
        print(
            f"  gate@{min_move:.0%} n={gate['n']:<5d} "
            f"cover={gate['coverage_pct']:.0f}%  "
            f"MAE={gate['mae_pct']:.2f}%  "
            f"dir={gate['directional_accuracy_pct']:.1f}%  "
            f"signed_ret={gate['signed_return_pct']:.2f}%"
        )
    print("=" * 60)
    print("MAE = mean absolute error. Average of |predicted % move − actual % move| over all windows. Lower = better.")
    print("Naive MAE = same metric but 'prediction' is 0% change (tomorrow = today, no move). Baseline for 'model add no value' case.")
    print("gate@min = same as live kronos_gate magnitude floor; dir/signed_ret only on windows that would clear it.")
    print()
