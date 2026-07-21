"""
learn/dataset.py — ingest the historical daily OHLCV CSVs and build the
feature/label matrix used to train the swing-win model.

Layout expected under `data_dir`: <FirstLetter>/<TICKER>.csv, columns
volume,low,close,open,high,timestamp (unix seconds). ~17.7k tickers / 3.8GB.

Processed per-ticker (float32, NaN rows dropped immediately) to keep peak
memory well under the full 3.8GB raw size — the feature matrix is a few GB
at most, not the whole CSV set held in memory at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd
from tqdm import tqdm

from learn.features import FEATURE_NAMES, compute_features
from learn.labels import triple_barrier_labels
from utils.logger import log

DEFAULT_DATA_DIR = Path("/home/r00t/stocks_data")
REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume", "timestamp"}


def iter_ticker_frames(data_dir: Path) -> Iterator[tuple[str, pd.DataFrame]]:
    """Yield (symbol, df) for every <letter>/<TICKER>.csv under data_dir.

    df has columns open, high, low, close, volume, indexed by timestamp,
    oldest-first, deduplicated.
    """
    for csv_path in sorted(data_dir.glob("*/*.csv")):
        symbol = csv_path.stem
        try:
            raw = pd.read_csv(csv_path)
        except Exception as exc:
            log.debug(f"learn | skipping {csv_path.name}: {exc}")
            continue
        if not REQUIRED_COLUMNS.issubset(raw.columns) or raw.empty:
            continue
        raw["timestamp"] = pd.to_datetime(raw["timestamp"], unit="s")
        df = (
            raw.sort_values("timestamp")
            .drop_duplicates("timestamp")
            .set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        )
        yield symbol, df


def build_dataset(
    data_dir: Path = DEFAULT_DATA_DIR,
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

    frames = iter_ticker_frames(data_dir)
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
