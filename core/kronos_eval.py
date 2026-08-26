"""
core/kronos_eval.py — orchestrates `python main.py --kronos-test`: run
Kronos-base (https://github.com/shiyu-coder/Kronos) walk-forward over the
historical daily CSVs in /home/r00t/stocks_data and score its +1 day /
+3 trading-day close-price forecasts against what actually happened.
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
# Confirm-gate contract: |predicted close-to-close| over this many trading days.
GATE_HORIZON_BARS = 3
WEEK_AHEAD = GATE_HORIZON_BARS  # historical name; horizon is 3d, not 5d/1w


_PRICE_COLS = ("open", "high", "low", "close")
_OHLCV_COLS = ("open", "high", "low", "close", "volume")


def with_amount(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → OHLCV+amount using KronosPredictor's documented fallback.

    When a feed has no real dollar-turnover column, the official predictor
    fills ``amount = volume * mean(OHLC)``. We materialize that here so gate,
    eval, and finetune all feed identical feature semantics.
    """
    out = df[["open", "high", "low", "close", "volume"]].copy()
    out["amount"] = out["volume"] * out[["open", "high", "low", "close"]].mean(axis=1)
    return out


def _naive_datetime_index(index) -> pd.DatetimeIndex | None:
    """DatetimeIndex Kronos ``.dt`` stamps can read. None if unusable."""
    try:
        ts = pd.to_datetime(pd.Series(index), errors="coerce")
    except Exception:
        return None
    if ts.isna().any() or len(ts) == 0:
        return None
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    return pd.DatetimeIndex(ts)


def _prep_3d_frame(df: pd.DataFrame, lookback: int):
    """Slice + amount + timestamps for a 3-trading-day forecast, or None.

    Output matches official ``predict`` / ``predict_batch``: OHLCV+amount, no
    NaN/inf, naive datetime stamps, ``len(x_df) == len(x_timestamp)``,
    ``len(y_timestamp) == GATE_HORIZON_BARS``.
    """
    if df is None or len(df) < 60:
        return None
    try:
        work = df.copy()
        idx = _naive_datetime_index(work.index)
        if idx is None:
            return None
        work.index = idx
        work = work.sort_index()
        work = work[~work.index.duplicated(keep="last")]
        for col in _OHLCV_COLS:
            if col not in work.columns:
                return None
            work[col] = pd.to_numeric(work[col], errors="coerce")
        if work[list(_PRICE_COLS)].isna().any().any():
            return None
        work["volume"] = work["volume"].fillna(0.0)
        vals = work[list(_OHLCV_COLS)].to_numpy(dtype=float)
        if not np.isfinite(vals).all():
            return None
        if (work[list(_PRICE_COLS)] <= 0).any().any():
            return None
        if len(work) < 60:
            return None
        use = min(lookback, len(work), MAX_CONTEXT)
        if use < 60:
            return None
        x_df = with_amount(work.iloc[-use:])
        last_close = float(x_df["close"].iloc[-1])
        if last_close <= 0 or not np.isfinite(last_close):
            return None
        if x_df.isna().any().any() or not np.isfinite(x_df.to_numpy(dtype=float)).all():
            return None
        x_timestamp = pd.Series(pd.DatetimeIndex(x_df.index))
        last = pd.Timestamp(x_df.index[-1])
        y_timestamp = pd.Series(
            pd.bdate_range(start=last + pd.Timedelta(days=1), periods=WEEK_AHEAD)
        )
        x_df = x_df.reset_index(drop=True)
        if len(x_df) != len(x_timestamp) or len(y_timestamp) != WEEK_AHEAD:
            return None
        if not hasattr(x_timestamp, "dt") or not hasattr(y_timestamp, "dt"):
            return None
        return x_df, x_timestamp, y_timestamp, last_close, len(x_df)
    except Exception:
        log.debug("Kronos | _prep_3d_frame rejected a frame", exc_info=True)
        return None


def _return_from_pred(pred_df: pd.DataFrame | None, last_close: float):
    if pred_df is None or len(pred_df) < WEEK_AHEAD or last_close <= 0:
        return None
    pred_1w = float(pred_df["close"].iloc[WEEK_AHEAD - 1]) / last_close - 1.0
    return pred_1w, last_close


def predict_1w_return(
    predictor,
    df: pd.DataFrame,
    *,
    sample_count: int = 1,
    lookback: int = LOOKBACK,
) -> tuple[float, float] | None:
    """Run Kronos on the last ``lookback`` bars; return (pred_3d, last_close).

    Return is close-to-close % over ``GATE_HORIZON_BARS`` (3 trading days, not 1w).
    Returns None if history is too short or predict fails.
    """
    prepared = _prep_3d_frame(df, lookback)
    if prepared is None:
        return None
    x_df, x_timestamp, y_timestamp, last_close, _use = prepared
    try:
        pred_df = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=WEEK_AHEAD,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )
        return _return_from_pred(pred_df, last_close)
    except Exception:
        log.exception("Kronos | predict_1w_return failed")
        return None


_WARNED_NO_PREDICT_BATCH = False
_CUDA_AVAILABLE: bool | None = None
_LOGGED_CPU_BATCH_CAP = False
# Attention is O(B * sample_count * heads * T^2). 16 is a CUDA default; CPU
# hosts (e.g. 8 GB Contabo) swap-thrash above a handful of series.
CPU_KRONOS_BATCH_CAP = 4


def _cuda_available() -> bool:
    global _CUDA_AVAILABLE
    if _CUDA_AVAILABLE is not None:
        return _CUDA_AVAILABLE
    try:
        import torch

        _CUDA_AVAILABLE = bool(torch.cuda.is_available())
    except Exception:
        _CUDA_AVAILABLE = False
    return _CUDA_AVAILABLE


def effective_kronos_batch_size(requested: int | None = None) -> int:
    """Chunk size for predict_batch. Caps on CPU when Batch Kronos is on."""
    global _LOGGED_CPU_BATCH_CAP
    want = requested
    if want is None:
        try:
            from config import settings

            want = settings.kronos_batch_size
        except Exception:
            want = 16
    want = max(1, int(want))
    if _cuda_available():
        return want
    got = min(want, CPU_KRONOS_BATCH_CAP)
    if got < want and not _LOGGED_CPU_BATCH_CAP:
        log.info(
            f"Kronos | no CUDA — batch_size {want} → {got} "
            f"(CPU cap {CPU_KRONOS_BATCH_CAP})"
        )
        _LOGGED_CPU_BATCH_CAP = True
    return got


def _is_cuda_oom(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("OutOfMemoryError", "CudaOutOfMemoryError"):
        return True
    msg = str(exc).lower()
    return "out of memory" in msg or ("cuda" in msg and "memory" in msg)


def _is_batch_contract_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "consistent historical length" in msg
        or "inconsistent lengths" in msg
        or "nan values" in msg
        or "missing price columns" in msg
        or "y_timestamp length" in msg
    )


def _validate_batch_payload(df_list, x_ts_list, y_ts_list, pred_len: int) -> str | None:
    """Return an error string if this chunk would trip official predict_batch."""
    if not (len(df_list) == len(x_ts_list) == len(y_ts_list)):
        return "list length mismatch"
    seq_lens: list[int] = []
    for i, (df, x_ts, y_ts) in enumerate(zip(df_list, x_ts_list, y_ts_list)):
        if not isinstance(df, pd.DataFrame):
            return f"index {i} is not a DataFrame"
        missing = [c for c in (*_PRICE_COLS, "volume", "amount") if c not in df.columns]
        if missing:
            return f"index {i} missing columns {missing}"
        cols = list((*_PRICE_COLS, "volume", "amount"))
        if df[cols].isna().any().any() or not np.isfinite(df[cols].to_numpy(dtype=float)).all():
            return f"index {i} has NaN/inf"
        x_ts = pd.Series(x_ts)
        y_ts = pd.Series(y_ts)
        if not hasattr(x_ts, "dt") or not hasattr(y_ts, "dt"):
            return f"index {i} timestamps are not datetime-like"
        try:
            pd.to_datetime(x_ts)
            pd.to_datetime(y_ts)
        except Exception:
            return f"index {i} timestamps are not parseable"
        if len(df) != len(x_ts):
            return f"index {i} x rows {len(df)} != x_timestamp {len(x_ts)}"
        if len(y_ts) != pred_len:
            return f"index {i} y_timestamp {len(y_ts)} != pred_len {pred_len}"
        seq_lens.append(len(df))
    if len(set(seq_lens)) != 1:
        return f"mixed historical lengths {seq_lens}"
    return None


def _predict_batch_chunk(predictor, items: list, sample_count: int) -> list:
    """Run predict_batch on one same-length chunk; split on CUDA OOM / contract errors."""
    if not items:
        return []
    df_list = [it[1] for it in items]
    x_ts_list = [it[2] for it in items]
    y_ts_list = [it[3] for it in items]
    bad = _validate_batch_payload(df_list, x_ts_list, y_ts_list, WEEK_AHEAD)
    if bad:
        if len(items) == 1:
            log.warning(f"Kronos | predict_batch skipped invalid series ({bad})")
            return [None]
        log.warning(f"Kronos | predict_batch payload invalid ({bad}) — splitting")
        mid = len(items) // 2
        return _predict_batch_chunk(
            predictor, items[:mid], sample_count
        ) + _predict_batch_chunk(predictor, items[mid:], sample_count)
    try:
        pred_dfs = predictor.predict_batch(
            df_list=df_list,
            x_timestamp_list=x_ts_list,
            y_timestamp_list=y_ts_list,
            pred_len=WEEK_AHEAD,
            T=1.0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )
    except Exception as exc:
        if len(items) > 1 and (_is_cuda_oom(exc) or _is_batch_contract_error(exc)):
            log.warning(
                f"Kronos | predict_batch failed at chunk={len(items)} ({exc}) — splitting"
            )
            mid = len(items) // 2
            return _predict_batch_chunk(
                predictor, items[:mid], sample_count
            ) + _predict_batch_chunk(predictor, items[mid:], sample_count)
        log.exception("Kronos | predict_batch chunk failed")
        return [None] * len(items)
    if pred_dfs is None or len(pred_dfs) != len(items):
        log.warning("Kronos | predict_batch returned mismatched length")
        return [None] * len(items)
    out = []
    for it, pred_df in zip(items, pred_dfs):
        out.append(_return_from_pred(pred_df, it[4]))
    return out


def predict_1w_return_batch(
    predictor,
    frames: list[pd.DataFrame],
    *,
    sample_count: int = 1,
    lookback: int = LOOKBACK,
    batch_size: int | None = None,
) -> list[tuple[float, float] | None]:
    """One ``(pred_3d, last_close)`` per frame, same 3% / 3d contract as sequential.

    ``pred_len`` is always ``GATE_HORIZON_BARS`` (3 trading days), never a calendar
    week. Groups by lookback length (official ``predict_batch`` requires identical
    seq_len), then chunks by ``effective_kronos_batch_size`` (config size on
    CUDA, capped on CPU). Falls back to sequential ``predict()`` if the
    predictor has no ``predict_batch``.
    """
    n = len(frames)
    results: list[tuple[float, float] | None] = [None] * n
    if n == 0:
        return results

    global _WARNED_NO_PREDICT_BATCH
    if not callable(getattr(predictor, "predict_batch", None)):
        if not _WARNED_NO_PREDICT_BATCH:
            log.warning(
                "Kronos | predictor has no predict_batch — sequential fallback"
            )
            _WARNED_NO_PREDICT_BATCH = True
        for i, df in enumerate(frames):
            results[i] = predict_1w_return(
                predictor, df, sample_count=sample_count, lookback=lookback,
            )
        return results

    prepared: list[tuple[int, object, object, object, float, int]] = []
    for i, df in enumerate(frames):
        item = _prep_3d_frame(df, lookback)
        if item is None:
            continue
        x_df, x_ts, y_ts, last_close, use = item
        prepared.append((i, x_df, x_ts, y_ts, last_close, use))

    chunk = effective_kronos_batch_size(batch_size)
    groups: dict[int, list] = {}
    for row in prepared:
        groups.setdefault(row[5], []).append(row[:5])

    for _use, group in groups.items():
        for start in range(0, len(group), chunk):
            part = group[start:start + chunk]
            pred_part = _predict_batch_chunk(predictor, part, sample_count)
            for (orig_i, *_rest), pred in zip(part, pred_part):
                results[orig_i] = pred
    return results


@dataclass
class WindowResult:
    symbol: str
    asof: pd.Timestamp
    actual_1d: float
    pred_1d: float
    actual_1w: float
    pred_1w: float
    # Prior GATE_HORIZON_BARS (3d) close-to-close return ending at asof.
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
    # Mean 3d return if always trading the predicted sign (unit notional).
    signed_ret = float(np.mean(np.sign(pred) * actual))
    return {
        "n": len(actual),
        "mae_pct": mae * 100,
        "naive_mae_pct": naive_mae * 100,
        "directional_accuracy_pct": float(np.mean(direction_hits)) * 100,
        "signed_return_pct": signed_ret * 100,
    }


def _score_persistence(results: list[WindowResult]) -> dict:
    """Same metrics as `_score`, but prediction = prior 3-trading-day return."""
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


def _candidates_from_history_api(min_bars: int) -> list[tuple[str, pd.DataFrame]]:
    """Load eval frames from GET /api/history when STOCKS_HISTORY_URL is set."""
    from data.history import bars_to_df
    from data.history_client import fetch_history_bars, fetch_history_symbols

    metas = fetch_history_symbols() or []
    eligible = [
        m for m in metas
        if (m.get("row_count") or 0) >= min_bars and m.get("symbol")
    ]
    # Cap fetches: full-universe HTTP would be huge. Rank by row_count then load.
    eligible.sort(key=lambda m: int(m.get("row_count") or 0), reverse=True)
    out: list[tuple[str, pd.DataFrame]] = []
    for meta in eligible[:400]:
        symbol = str(meta["symbol"]).upper()
        bars = fetch_history_bars(symbol)
        df = bars_to_df(bars)
        if df is None or len(df) < min_bars:
            continue
        out.append((symbol, df))
    log.info(f"Kronos-test | history API loaded {len(out)} tickers with >= {min_bars} bars")
    return out


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

    from data.history_client import history_api_configured

    if history_api_configured():
        candidates = _candidates_from_history_api(min_bars)
        if not candidates:
            log.error("Kronos-test | history API returned no tickers with enough bars")
            return
    else:
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
        min_move = 0.03
    gate = score_gate_rule(all_results, min_move)
    persist = _score_persistence(all_results)

    print()
    print("=" * 60)
    print(f"  Kronos-base accuracy — {len(candidates)} symbols, {len(all_results)} windows")
    print("=" * 60)
    for label, s in (("+1 day", score_1d), ("+3 days", score_1w)):
        print(f"  {label:8s}  n={s['n']:<5d} "
              f"MAE={s['mae_pct']:.2f}%  (naive MAE={s['naive_mae_pct']:.2f}%)  "
              f"direction hit={s['directional_accuracy_pct']:.1f}%")
    if persist.get("n"):
        print(
            f"  persist  n={persist['n']:<5d} "
            f"MAE={persist['mae_pct']:.2f}%  "
            f"direction hit={persist['directional_accuracy_pct']:.1f}%  "
            f"(prior 3d return baseline)"
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
