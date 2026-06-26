"""
patterns/base_pattern.py — Abstract base class for all trading patterns.

KEY CHANGE from webhook architecture:
  Patterns are no longer reactive (waiting for an alert).
  They are ANALYTICAL — called every scan cycle with fresh market data,
  and must decide independently whether a signal exists.

How to add a new pattern (one per file):
  1. Create patterns/pattern_00X_name.py
  2. Subclass BasePattern
  3. Set `name` and `timeframes` (which intervals to watch)
  4. Implement `analyze()` — return a TradeSignal or None
  5. Optionally override `chart_description` for the vision prompt
  6. Done — the scanner auto-discovers it
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from utils.logger import log


@dataclass
class TradeSignal:
    """
    Returned by a pattern's analyze() when it detects a valid setup.
    confidence: 0.0–1.0 — how strongly the indicators support the trade.
    If confidence < settings.vision_min_indicator_confidence, vision check is skipped.
    """
    symbol:      str
    action:      Literal["BUY", "SELL", "CLOSE"]
    pattern:     str
    timeframe:   str
    confidence:  float           # 0.0 – 1.0
    price:       float           # estimated entry price
    qty:         float           # number of shares/contracts
    stop_loss:   float | None = None
    take_profit: float | None = None
    notes:       str = ""


class BasePattern(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique pattern ID, e.g. 'pattern_001_ema_crossover'."""

    @property
    def timeframes(self) -> list[str]:
        """
        Timeframes this pattern wants to be called for.
        Override to restrict which intervals trigger this pattern.
        Default: hourly only.
        """
        return ["1h"]

    @property
    def chart_description(self) -> str:
        """
        Human-readable description of what this pattern looks like on a chart.
        Used in the vision confirmation prompt. Override with Toby's description.
        """
        return f"The {self.name} pattern."

    @abstractmethod
    def analyze(
        self,
        snapshot: MarketSnapshot,
        store: OHLCVStore,
    ) -> TradeSignal | None:
        """
        Called every scan cycle for each symbol + timeframe combination.

        Args:
            snapshot: Current bar data + TradingView indicators for this symbol/timeframe.
            store:    Rolling candle history — call store.get_df(symbol, tf) for a DataFrame.

        Returns:
            TradeSignal if a valid setup is detected, or None if no signal.

        Guidelines:
          - Keep logic focused on ONE pattern per class.
          - Use snapshot.indicator("EMA20") for TV-computed values.
          - Use store.get_df() + IndicatorEngine for custom indicator computation.
          - Set confidence honestly — it gates the expensive vision check.
          - Log key decision points with log.debug() for Toby's review.
        """

    # ── Optional lifecycle hooks ───────────────────────────────────────────────
    def on_start(self) -> None:
        """Called once when the scanner starts."""

    def on_stop(self) -> None:
        """Called once when the scanner stops."""

    def __repr__(self) -> str:
        return f"<Pattern: {self.name} | timeframes={self.timeframes}>"
