"""
learn/dataset.py — build the feature/label matrix from stocks_history.

Ticker frames come from GET /api/history, never CSV or local Postgres.
Processed per-ticker (float32, NaN rows dropped immediately).
"""

from __future__ import annotations

from typing import Iterator

import numpy as np
import pandas as pd
from tqdm import tqdm

from learn.features import FEATURE_NAMES, compute_features
from learn.labels import triple_barrier_labels


def iter_ticker_frames(
    *,
    min_bars: int = 0,
    max_tickers: int | None = None,
) -> Iterator[tuple[str, pd.DataFrame]]:
    """Yield (symbol, df) for every stocks_history ticker with enough bars.

    df has columns open, high, low, close, volume, datetime index, oldest-first.
    """
    from data.history import list_history_symbols, load_daily_ohlcv_df

    metas = list_history_symbols()
    n = 0
    for meta in metas:
        symbol = str(meta.get("symbol") or "").upper()
        if not symbol:
            continue
        if min_bars and int(meta.get("row_count") or 0) < min_bars:
            continue
        df = load_daily_ohlcv_df(symbol)
        if df is None or df.empty:
            continue
        if min_bars and len(df) < min_bars:
            continue
        yield symbol, df
        n += 1
        if max_tickers is not None and n >= max_tickers:
            return


def build_dataset(
    horizon_days: int = 10,
    target_pct: float = 0.09,
    stop_pct: float = 0.06,
    min_bars: int = 280,
    max_tickers: int | None = None,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Returns (X, y, n_tickers_used). X is float32 [n_examples, len(FEATURE_NAMES)]."""
    x_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    n_tickers = 0

    frames = iter_ticker_frames(min_bars=min_bars, max_tickers=max_tickers)
    for symbol, df in tqdm(frames, desc="Ingesting tickers", unit="ticker"):
        if len(df) < min_bars:
            continue
        feats = compute_features(df)
        labels = triple_barrier_labels(df, horizon_days, target_pct, stop_pct)
        valid = feats.notna().all(axis=1) & labels.notna()
        if not valid.any():
            continue
        x_parts.append(feats.loc[valid, FEATURE_NAMES].to_numpy(dtype=np.float32))
        y_parts.append(labels.loc[valid].to_numpy(dtype=np.float32))
        n_tickers += 1
        if max_tickers is not None and n_tickers >= max_tickers:
            break

    if not x_parts:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float32), np.empty(0, dtype=np.float32), 0

    X = np.concatenate(x_parts, axis=0)
    y = np.concatenate(y_parts, axis=0)
    return X, y, n_tickers
