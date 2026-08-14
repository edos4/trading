"""
data/ohlcv_store.py — Rolling in-memory candle store.

Each scan appends the latest candle. Patterns that need N bars of history
(e.g. to compute a 20-period EMA themselves, or to detect multi-bar patterns)
pull a DataFrame from here instead of re-fetching from TradingView.

Key: (symbol, timeframe) → deque of OHLCVCandle, max length = WINDOW
"""

from __future__ import annotations
from collections import deque
from dataclasses import asdict

import pandas as pd

from data.tv_client import OHLCVCandle, MarketSnapshot
from utils.logger import log

# How many candles to keep in memory per symbol/timeframe
# Sized for Kronos gate LOOKBACK=400 (+ headroom) and TV history ≤512.
DEFAULT_WINDOW = 512


class OHLCVStore:
    def __init__(self, window: int = DEFAULT_WINDOW, session_tz: str = "America/New_York"):
        self._window = window
        self._session_tz = session_tz or "America/New_York"
        # {(symbol, timeframe): deque[OHLCVCandle]}
        self._store: dict[tuple[str, str], deque[OHLCVCandle]] = {}
        # {(symbol, timeframe): date of the last pushed snapshot} — used to
        # tell "still the same bar, price ticked" from "a new bar printed"
        # without relying on candle close price (see push()).
        self._last_push_date: dict[tuple[str, str], object] = {}
        # get_df() rebuilds a DataFrame from the deque every call — expensive
        # when called several times per bar (regime filter, ATR sizing, every
        # pattern's analyze()). Cache it per key, invalidated on any mutation.
        self._version: dict[tuple[str, str], int] = {}
        self._df_cache: dict[tuple[str, str], tuple[int, pd.DataFrame]] = {}

    def _bump(self, key: tuple[str, str]) -> None:
        self._version[key] = self._version.get(key, 0) + 1

    def push(self, snapshot: MarketSnapshot) -> None:
        """Append the latest candle from a snapshot.

        Used as a fallback when bulk history can't be (re)fetched, so we only
        have one fresh candle per call. We need to decide whether this candle
        is an in-progress update to the bar we already have (same session) or
        the start of a new bar (a new session's candle).

        Comparing `close` price to make that call is unreliable: two
        different sessions can legitimately close at the same price (silent
        overwrite, losing a bar from history), and the same still-forming
        session's price ticking between calls looks like "a new bar"
        (spurious duplicate bar). Compare by the snapshot's fetch date
        instead — for daily/weekly bars, two pushes on the same calendar day
        are the same bar.
        """
        key = (snapshot.symbol, snapshot.timeframe)
        if key not in self._store:
            self._store[key] = deque(maxlen=self._window)
        candles = self._store[key]
        push_date = snapshot.timestamp.date() if snapshot.timestamp else None
        same_session = (
            candles
            and push_date is not None
            and self._last_push_date.get(key) == push_date
        )
        if same_session:
            candles[-1] = snapshot.candle
        else:
            candles.append(snapshot.candle)
        if push_date is not None:
            self._last_push_date[key] = push_date
        self._bump(key)

    def append_candle(self, symbol: str, timeframe: str, candle: OHLCVCandle) -> None:
        """Append a single raw candle (backtest walk-forward — no session logic)."""
        key = (symbol, timeframe)
        if key not in self._store:
            self._store[key] = deque(maxlen=self._window)
        self._store[key].append(candle)
        self._bump(key)

    def replace_all(
        self, symbol: str, timeframe: str, candles: list[OHLCVCandle]
    ) -> None:
        """Replace stored history (used when refreshing from screener offsets)."""
        if not candles:
            return
        key = (symbol, timeframe)
        self._store[key] = deque(candles[-self._window :], maxlen=self._window)
        self._bump(key)

    def get_df(
        self, symbol: str, timeframe: str, min_bars: int = 2
    ) -> pd.DataFrame | None:
        """
        Return stored candles as a DataFrame with columns:
        open, high, low, close, volume  (oldest first)
        Returns None if fewer than min_bars candles are available.
        """
        key = (symbol, timeframe)
        candles = self._store.get(key)
        if not candles or len(candles) < min_bars:
            log.debug(f"OHLCVStore | Not enough history for {symbol} {timeframe} yet")
            return None
        version = self._version.get(key, 0)
        cached = self._df_cache.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        records = [asdict(c) for c in candles]
        df = pd.DataFrame(records)
        if "timestamp" in df.columns and df["timestamp"].notna().any():
            idx = pd.to_datetime(df["timestamp"])
            tz = self._session_tz
            if idx.dt.tz is not None:
                idx = idx.dt.tz_convert(tz)
            else:
                idx = idx.dt.tz_localize(tz)
            df.index = idx.dt.tz_localize(None).dt.normalize()
            df = df.drop(columns=["timestamp"])
        df = df[["open", "high", "low", "close", "volume"]]
        self._df_cache[key] = (version, df)
        return df

    def latest_close(self, symbol: str, timeframe: str) -> float | None:
        key = (symbol, timeframe)
        candles = self._store.get(key)
        return candles[-1].close if candles else None

    def latest_candle(self, symbol: str, timeframe: str) -> OHLCVCandle | None:
        key = (symbol, timeframe)
        candles = self._store.get(key)
        return candles[-1] if candles else None

    def available(self, symbol: str, timeframe: str) -> int:
        """How many candles are currently stored."""
        return len(self._store.get((symbol, timeframe), []))
