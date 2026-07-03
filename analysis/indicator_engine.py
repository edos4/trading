"""
analysis/indicator_engine.py — Computes technical indicators from OHLCV DataFrames.

tradingview_ta already gives us pre-computed indicator values in the snapshot,
but this module lets patterns compute their OWN versions, back-test against
historical candles, or derive custom signals that TV doesn't expose.

Usage:
    from analysis.indicator_engine import IndicatorEngine
    ind = IndicatorEngine(ohlcv_df)
    ema_9  = ind.ema(9)
    rsi_14 = ind.rsi(14)
    macd   = ind.macd()
"""

from __future__ import annotations
import numpy as np
import pandas as pd


class IndicatorEngine:
    def __init__(self, df: pd.DataFrame):
        """
        df must have columns: open, high, low, close, volume
        Rows are oldest-first.
        """
        self._df = df.copy().reset_index(drop=True)
        self.close  = self._df["close"]
        self.high   = self._df["high"]
        self.low    = self._df["low"]
        self.open   = self._df["open"]
        self.volume = self._df["volume"]

    # ── Moving averages ────────────────────────────────────────────────────────
    def ema(self, period: int) -> pd.Series:
        return self.close.ewm(span=period, adjust=False).mean()

    def sma(self, period: int) -> pd.Series:
        return self.close.rolling(window=period).mean()

    def wma(self, period: int) -> pd.Series:
        weights = np.arange(1, period + 1)
        return self.close.rolling(period).apply(
            lambda x: np.dot(x, weights) / weights.sum(), raw=True
        )

    # ── Momentum ───────────────────────────────────────────────────────────────
    def rsi(self, period: int = 14) -> pd.Series:
        delta = self.close.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def rsi_wilder(self, period: int = 14) -> pd.Series:
        """Wilder's RSI: SMA seed then recursive Wilder smoothing.

        Differs from `rsi` (which uses a simple rolling mean of gains/losses).
        Used by patterns whose locked spec calls for Wilder RSI (e.g. head &
        shoulders).
        """
        close = self.close.to_numpy(dtype=float)
        n = len(close)
        out = np.full(n, np.nan)
        if n <= period:
            return pd.Series(out, index=self._df.index)
        delta = np.diff(close)  # length n-1, delta[k] = close[k+1]-close[k]
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        # Seed: SMA of the first `period` gains/losses (covers bars 1..period).
        ag = np.full(n, np.nan)
        al = np.full(n, np.nan)
        ag[period] = gain[:period].mean()
        al[period] = loss[:period].mean()
        for i in range(period + 1, n):
            ag[i] = (ag[i - 1] * (period - 1) + gain[i - 1]) / period
            al[i] = (al[i - 1] * (period - 1) + loss[i - 1]) / period
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = ag / np.where(al == 0, np.nan, al)
        rsi = 100 - (100 / (1 + rs))
        # Wilder edge cases: no losses → RSI 100; no gains → RSI 0.
        rsi = np.where(np.isfinite(ag) & (al == 0), 100.0, rsi)
        rsi = np.where(np.isfinite(ag) & (ag == 0) & (al > 0), 0.0, rsi)
        out = rsi
        return pd.Series(out, index=self._df.index)

    def macd(
        self, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (macd_line, signal_line, histogram)."""
        ema_fast   = self.close.ewm(span=fast, adjust=False).mean()
        ema_slow   = self.close.ewm(span=slow, adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram  = macd_line - signal_line
        return macd_line, signal_line, histogram

    def stoch(self, k_period: int = 14, d_period: int = 3) -> tuple[pd.Series, pd.Series]:
        """Returns (%K, %D)."""
        lowest  = self.low.rolling(k_period).min()
        highest = self.high.rolling(k_period).max()
        k = 100 * (self.close - lowest) / (highest - lowest).replace(0, np.nan)
        d = k.rolling(d_period).mean()
        return k, d

    # ── Volatility ─────────────────────────────────────────────────────────────
    def atr(self, period: int = 14) -> pd.Series:
        hl  = self.high - self.low
        hcp = (self.high - self.close.shift()).abs()
        lcp = (self.low  - self.close.shift()).abs()
        tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def bollinger_bands(
        self, period: int = 20, std_dev: float = 2.0
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (upper_band, middle_band, lower_band)."""
        mid   = self.sma(period)
        std   = self.close.rolling(period).std()
        upper = mid + std_dev * std
        lower = mid - std_dev * std
        return upper, mid, lower

    # ── Volume ─────────────────────────────────────────────────────────────────
    def obv(self) -> pd.Series:
        direction = np.sign(self.close.diff()).fillna(0)
        return (direction * self.volume).cumsum()

    def vwap(self) -> pd.Series:
        typical = (self.high + self.low + self.close) / 3
        return (typical * self.volume).cumsum() / self.volume.cumsum()

    # ── Candlestick helpers ────────────────────────────────────────────────────
    def is_bullish_candle(self, idx: int = -1) -> bool:
        return self.close.iloc[idx] > self.open.iloc[idx]

    def is_bearish_candle(self, idx: int = -1) -> bool:
        return self.close.iloc[idx] < self.open.iloc[idx]

    def body_size(self, idx: int = -1) -> float:
        return abs(self.close.iloc[idx] - self.open.iloc[idx])

    def wick_upper(self, idx: int = -1) -> float:
        return self.high.iloc[idx] - max(self.close.iloc[idx], self.open.iloc[idx])

    def wick_lower(self, idx: int = -1) -> float:
        return min(self.close.iloc[idx], self.open.iloc[idx]) - self.low.iloc[idx]
