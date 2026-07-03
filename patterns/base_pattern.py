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
from dataclasses import dataclass, field
from typing import Literal

from data.tv_client import MarketSnapshot
from data.ohlcv_store import OHLCVStore
from utils.logger import log


# ── Chart annotation colors (shared so every pattern draws consistently) ──────
ANN_PEAK   = "#ef5350"   # swing high / short-side structure
ANN_TROUGH = "#26a69a"   # swing low  / long-side  structure
ANN_ENTRY  = "#ffeb3b"   # entry marker
ANN_LINE   = "#ff9800"   # neckline / channel lines
ANN_STOP   = "#ef5350"   # stop-loss horizontal
ANN_TARGET = "#26a69a"   # take-profit horizontal
ANN_REF    = "#2962ff"   # reference points (channel start, etc.)


@dataclass
class TradeSignal:
    """
    Returned by a pattern's analyze() when it detects a valid setup.
    confidence: 0.0–1.0 — how strongly the indicators support the trade.
    If confidence < settings.vision_min_indicator_confidence, vision check is skipped.

    chart_annotations: optional list of drawable elements the chart renderer
    overlays on the PNG so a human (or the vision model) can see the pattern.
    Each element is a dict:
      {"type": "marker",  "date": "YYYY-MM-DD", "price": float,
       "label": str, "color": hex, "marker": "^"|"v"|"o"|"x",
       "label_pos": "above"|"below"}
      {"type": "hline",   "price": float, "label": str,
       "color": hex, "style": "--"|"-."|":"}
      {"type": "segment", "start_date": ..., "end_date": ...,
       "start_price": float, "end_price": float,
       "color": hex, "style": "-"|"--", "width": float}
    Dates must match the OHLCVStore DataFrame index (normalized session dates).
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
    trailing_stop_pct: float | None = None
    trailing_stop_mode: Literal[
        "lowest_close", "highest_close", "lowest_low", "highest_high"
    ] | None = None
    neckline: float | None = None
    neckline_break_direction: Literal["below", "above"] | None = None
    exit_bars_after_neckline_break: int | None = None
    notes:       str = ""
    chart_annotations: list[dict] = field(default_factory=list)


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

    # ── Chart annotation helpers ───────────────────────────────────────────────
    @staticmethod
    def bar_date(df, idx: int) -> str:
        """ISO date string for bar `idx` in `df`, for chart annotations.

        The OHLCVStore DataFrame uses a normalized session DatetimeIndex; we
        emit "YYYY-MM-DD" so the chart renderer can locate the bar after it
        trims to its visible window.
        """
        ts = df.index[idx]
        try:
            return ts.strftime("%Y-%m-%d")
        except AttributeError:
            return str(ts)

    def __repr__(self) -> str:
        return f"<Pattern: {self.name} | timeframes={self.timeframes}>"


# ── Annotation builders (keep pattern code terse & consistent) ────────────────
def ann_marker(
    date: str, price: float, label: str, color: str,
    marker: str = "o", label_pos: str = "above",
) -> dict:
    return {
        "type": "marker", "date": date, "price": price, "label": label,
        "color": color, "marker": marker, "label_pos": label_pos,
    }


def ann_hline(price: float, label: str, color: str, style: str = "--") -> dict:
    return {"type": "hline", "price": price, "label": label, "color": color, "style": style}


def ann_segment(
    d0: str, d1: str, p0: float, p1: float, color: str,
    style: str = "-", width: float = 1.4,
) -> dict:
    return {
        "type": "segment", "start_date": d0, "end_date": d1,
        "start_price": p0, "end_price": p1,
        "color": color, "style": style, "width": width,
    }
