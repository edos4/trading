"""
learn/features.py — feature engineering shared by training (learn/dataset.py)
and live inference (patterns/012_ml_signal.py).

Sharing this one function is the whole point: if training and the live
pattern ever computed features differently, the model would predict on a
distribution it never saw during training. Every feature here uses only
data up to and including the current bar (no lookahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.indicator_engine import IndicatorEngine

FEATURE_NAMES: list[str] = [
    "ret_1", "ret_5", "ret_10", "ret_20",
    "sma10_ratio", "sma20_ratio", "sma50_ratio",
    "rsi_14", "atr_pct", "volatility_20",
    "volume_z_20", "range_pct_10",
    "dist_252_high", "dist_252_low",
]

# Bars needed before the shortest-warmup feature (rsi_14/atr_pct) stabilizes.
# The 252-bar features use min_periods so they degrade gracefully instead of
# staying NaN for a full year of history.
MIN_BARS_REQUIRED = 60


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """df must have columns open, high, low, close, volume, oldest-first.

    Returns a DataFrame indexed like `df` with columns FEATURE_NAMES.
    """
    ind = IndicatorEngine(df)
    close, high, low, volume = ind.close, ind.high, ind.low, ind.volume

    vol_mean_20 = volume.rolling(20, min_periods=10).mean()
    vol_std_20 = volume.rolling(20, min_periods=10).std()

    feats = pd.DataFrame(index=df.index)
    feats["ret_1"] = close.pct_change(1, fill_method=None).to_numpy()
    feats["ret_5"] = close.pct_change(5, fill_method=None).to_numpy()
    feats["ret_10"] = close.pct_change(10, fill_method=None).to_numpy()
    feats["ret_20"] = close.pct_change(20, fill_method=None).to_numpy()
    feats["sma10_ratio"] = (close / ind.sma(10) - 1).to_numpy()
    feats["sma20_ratio"] = (close / ind.sma(20) - 1).to_numpy()
    feats["sma50_ratio"] = (close / ind.sma(50) - 1).to_numpy()
    feats["rsi_14"] = ind.rsi(14).to_numpy()
    feats["atr_pct"] = (ind.atr(14) / close).to_numpy()
    feats["volatility_20"] = (
        close.pct_change(fill_method=None).rolling(20, min_periods=10).std().to_numpy()
    )
    feats["volume_z_20"] = ((volume - vol_mean_20) / vol_std_20.replace(0, np.nan)).to_numpy()
    feats["range_pct_10"] = ((high - low) / close).rolling(10, min_periods=5).mean().to_numpy()
    feats["dist_252_high"] = (close / close.rolling(252, min_periods=100).max() - 1).to_numpy()
    feats["dist_252_low"] = (close / close.rolling(252, min_periods=100).min() - 1).to_numpy()
    return feats[FEATURE_NAMES]
