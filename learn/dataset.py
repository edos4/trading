"""
learn/dataset.py — build the feature/label matrix from stocks_history.

Ticker frames come from GET /api/history, never CSV or local Postgres.
Processed per-ticker (float32, NaN rows dropped immediately).
"""

from __future__ import annotations

from typing import Iterator

import pandas as pd


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
