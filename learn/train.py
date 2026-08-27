"""
learn/train.py — orchestrates `python main.py --learn`: load stocks_history,
train the swing-win LightGBM classifier, save it under models/.
"""

from __future__ import annotations

from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np

from learn.dataset import build_dataset
from learn.features import FEATURE_NAMES
from learn.model import save_model
from utils.logger import log

DEFAULT_HORIZON_DAYS = 10
DEFAULT_TARGET_PCT = 0.09
DEFAULT_STOP_PCT = 0.06


def _train_lgb(X: np.ndarray, y: np.ndarray, seed: int = 42, val_frac: float = 0.15):
    rng = np.random.default_rng(seed)
    n = len(y)
    perm = rng.permutation(n)
    cut = int(n * (1 - val_frac))
    train_idx, val_idx = perm[:cut], perm[cut:]

    train_set = lgb.Dataset(X[train_idx], label=y[train_idx], feature_name=FEATURE_NAMES)
    val_set = lgb.Dataset(
        X[val_idx], label=y[val_idx], feature_name=FEATURE_NAMES, reference=train_set,
    )
    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 200,
        "verbose": -1,
    }
    booster = lgb.train(
        params,
        train_set,
        num_boost_round=500,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    auc = booster.best_score["valid_0"]["auc"]
    return booster, auc


def run_learn(
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
    max_tickers: int | None = None,
) -> None:
    log.info(
        f"Learn | stocks_history | horizon={horizon_days}d "
        f"target={target_pct:.1%} stop={stop_pct:.1%}"
        + (f" | max_tickers={max_tickers}" if max_tickers else "")
    )
    X, y, n_tickers = build_dataset(
        horizon_days=horizon_days,
        target_pct=target_pct,
        stop_pct=stop_pct,
        max_tickers=max_tickers,
    )
    if len(y) == 0:
        log.error("Learn | no usable examples found — check stocks_history")
        return
    win_rate = float(y.mean())
    log.info(
        f"Learn | dataset built: {len(y):,} examples from {n_tickers:,} tickers, "
        f"win_rate={win_rate:.3f}"
    )
    booster, auc = _train_lgb(X, y)
    log.info(f"Learn | trained | val AUC={auc:.4f}")

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_examples": len(y),
        "n_tickers": n_tickers,
        "win_rate": win_rate,
        "val_auc": auc,
        "horizon_days": horizon_days,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "feature_names": FEATURE_NAMES,
    }
    save_model(booster, meta)
    log.info("Learn | model saved to models/pattern_012_ml_signal.txt")
