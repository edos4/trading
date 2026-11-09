"""Causal Kronos context selection after extreme stock-price collapses."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

POLICY_VERSION = 'post-price-collapse-v2'
MIN_CONTEXT_BARS = 60
PRICE_BREAK_RATIO = 10.0
OHLCV = ('open', 'high', 'low', 'close', 'volume')


@dataclass(frozen=True)
class ModelContext:
    frame: pd.DataFrame
    original_bars: int
    trimmed_bars: int
    regime_start: str | None

    def evidence(self) -> dict:
        return {'policy': POLICY_VERSION, 'original_bars': self.original_bars,
                'used_bars': len(self.frame), 'trimmed_bars': self.trimmed_bars,
                'regime_start': self.regime_start}


def prepare_context(frame: pd.DataFrame, lookback: int = 400, *, min_bars: int = MIN_CONTEXT_BARS) -> ModelContext:
    """Use only the latest observed price regime; never inspect forecast labels.

    A close still more than ten times the anchor belongs to the old scale,
    whether that drop happened in one session or across many. Start after the
    latest such close and keep at least the native model's minimum context.
    A newer regime with too little history abstains. Prices and predictions
    are never clipped or rewritten.
    """
    if frame is None or not isinstance(frame.index, pd.DatetimeIndex) or not all(c in frame for c in OHLCV):
        raise ValueError('Invalid OHLC context: require dated OHLCV bars')
    if lookback < min_bars:
        raise ValueError(f'Kronos requires at least {min_bars} context bars')
    work = frame.copy().sort_index(kind='stable')
    work = work[~work.index.duplicated(keep='last')].tail(min(int(lookback), 512))
    work[list(OHLCV)] = work[list(OHLCV)].apply(pd.to_numeric, errors='coerce')
    work['volume'] = work['volume'].fillna(0.)
    original = len(work)
    closes = work.close.to_numpy(dtype=float)
    if len(work) < min_bars or not np.isfinite(closes).all() or (closes <= 0).any():
        raise ValueError('Invalid OHLC context: insufficient finite positive close history')
    old_scale = np.flatnonzero(closes[:-1] > PRICE_BREAK_RATIO * closes[-1])
    start = int(old_scale[-1] + 1) if len(old_scale) else 0
    if original - start < min_bars:
        raise ValueError(f'Insufficient post-price-break context: {original-start} bars; need {min_bars}')
    work = work.iloc[start:].copy()
    values = work[list(OHLCV)].to_numpy(dtype=float)
    prices = work[list(OHLCV[:4])]
    if (work.index.isna().any() or not np.isfinite(values).all() or (prices <= 0).any().any()
            or (work.volume < 0).any() or (work.high < prices.max(axis=1)).any()
            or (work.low > prices.min(axis=1)).any()):
        raise ValueError('Invalid OHLC context: require valid ranges and nonnegative volume')
    return ModelContext(work, original, start, work.index[0].date().isoformat() if start else None)
