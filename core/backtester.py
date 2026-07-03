"""
core/backtester.py — Historical walk-forward backtest engine.

Replays historical OHLCV data through all registered patterns bar-by-bar,
simulating entries, exits, and position management. No live data, no MCP,
no TradingView indicators — relies purely on IndicatorEngine-computed values.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

import patterns as patterns_pkg
from config import settings
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.tv_client import TVClient, MarketSnapshot, OHLCVCandle, SCREENER_FIELDS
from patterns.base_pattern import BasePattern, TradeSignal
from utils.logger import log


@dataclass
class BacktestTrade:
    symbol: str
    timeframe: str
    pattern: str
    action: Literal["BUY", "SELL"]
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    stop_loss: float | None = None
    take_profit: float | None = None
    # Path-dependent exit state (populated from TradeSignal).
    neckline: float | None = None
    neckline_break_direction: Literal["below", "above"] | None = None
    exit_bars_after_neckline_break: int | None = None
    trailing_stop_pct: float | None = None
    trailing_stop_mode: Literal["lowest_close", "highest_close"] | None = None
    entry_bar_idx: int = -1
    neckline_break_bar_idx: int | None = None
    lowest_close_since_entry: float | None = None
    highest_close_since_entry: float | None = None
    lowest_low_since_entry: float | None = None
    highest_high_since_entry: float | None = None
    exit_reason: str = ""

    def __str__(self) -> str:
        return (
            f"{self.entry_date.strftime('%Y-%m-%d')} "
            f"{self.action:4s} {self.symbol:6s} {self.timeframe} "
            f"entry={self.entry_price:.2f} exit={self.exit_price:.2f} "
            f"pnl={self.pnl_pct:+.2f}% ({self.exit_reason})"
        )


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    total_signals: int = 0

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def loss_count(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)

    @property
    def win_rate(self) -> float:
        return self.win_count / len(self.trades) if self.trades else 0.0

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def avg_pnl_pct(self) -> float:
        return self.total_pnl_pct / len(self.trades) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        cumulative = np.cumsum([0.0] + [t.pnl_pct for t in self.trades])
        peak = np.maximum.accumulate(cumulative)
        drawdown = cumulative - peak
        return float(drawdown.min())

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        returns = np.array([t.pnl_pct for t in self.trades])
        mean = returns.mean()
        std = returns.std(ddof=1)
        return float(mean / std * np.sqrt(252)) if std > 0 else 0.0

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Total signals:  {self.total_signals}",
            f"  Trades taken:   {len(self.trades)}",
            f"  Wins:           {self.win_count}",
            f"  Losses:         {self.loss_count}",
            f"  Win rate:       {self.win_rate:.1%}",
            f"  Total P&L:      {self.total_pnl_pct:+.2f}%",
            f"  Avg P&L/trade:  {self.avg_pnl_pct:+.2f}%",
            f"  Max drawdown:   {self.max_drawdown_pct:+.2f}%",
            f"  Sharpe ratio:   {self.sharpe_ratio:.2f}",
            "=" * 60,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_signals": self.total_signals,
            "trades_taken": len(self.trades),
            "wins": self.win_count,
            "losses": self.loss_count,
            "win_rate": round(self.win_rate, 4),
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "avg_pnl_pct": round(self.avg_pnl_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "trades": [
                {
                    "symbol": t.symbol,
                    "timeframe": t.timeframe,
                    "pattern": t.pattern,
                    "action": t.action,
                    "entry_date": t.entry_date.isoformat(),
                    "exit_date": t.exit_date.isoformat(),
                    "entry_price": round(t.entry_price, 4),
                    "exit_price": round(t.exit_price, 4),
                    "pnl_pct": round(t.pnl_pct, 4),
                    "exit_reason": t.exit_reason,
                    "stop_loss": round(t.stop_loss, 4) if t.stop_loss else None,
                    "take_profit": round(t.take_profit, 4) if t.take_profit else None,
                }
                for t in self.trades
            ],
        }

    def save(self, path: str) -> None:
        p = Path(path)
        p.write_text(self.summary() + "\n", encoding="utf-8")
        log.info(f"Backtest | Results saved to {p}")


class Backtester:
    def __init__(self, symbols: list[str]):
        self._symbols = symbols
        self._tv = TVClient(settings.tv_screener, settings.tv_exchange)
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._discover_patterns()

    def _discover_patterns(self) -> None:
        for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
            if module_info.name.startswith("pattern_"):
                module = importlib.import_module(f"patterns.{module_info.name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BasePattern)
                        and attr is not BasePattern
                    ):
                        instance = attr()
                        self._patterns.append(instance)
                        self._pattern_files[instance.name] = (
                            f"patterns/{module_info.name}.py"
                        )
                        log.info(f"Backtester | Registered pattern: {instance}")

    async def run(self) -> BacktestResult:
        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        result = BacktestResult()

        for symbol in self._symbols:
            for timeframe in sorted(all_timeframes):
                trades, signals = self._backtest_symbol(symbol, timeframe)
                result.trades.extend(trades)
                result.total_signals += signals

        return result

    def _backtest_symbol(
        self, symbol: str, timeframe: str
    ) -> tuple[list[BacktestTrade], int]:
        candles = self._fetch_history(symbol, timeframe)
        if len(candles) < 1:
            return [], 0

        store = OHLCVStore(window=max(DEFAULT_WINDOW, len(candles)))
        trades: list[BacktestTrade] = []
        signals_count = 0

        pending_entry: TradeSignal | None = None
        open_position: BacktestTrade | None = None
        min_bars = self._min_required_bars(timeframe)
        i = max(min_bars, 1)

        while i < len(candles):
            window_candles = candles[: i + 1]
            store.replace_all(symbol, timeframe, window_candles)

            # Exit check for open position at current bar
            if open_position is not None:
                self._update_neckline_state(open_position, candles[i], i)
                exit_price, exit_reason = self._check_exit(
                    candles[i], open_position, i
                )
                if exit_price is not None:
                    self._close_trade(
                        open_position, exit_price, exit_reason, candles[i]
                    )
                    trades.append(open_position)
                    open_position = None
                    i += 1
                    continue
                # No exit this bar → fold current close into trailing reference
                # so it is available as a stop level on the next bar.
                self._update_trailing_reference(open_position, candles[i])
                i += 1
                continue

            # Enter pending position at this bar
            if pending_entry is not None:
                open_position = self._open_trade(pending_entry, candles[i], i)
                pending_entry = None
                i += 1
                continue

            # Detect signals at current bar
            snapshot = self._make_snapshot(symbol, timeframe, candles[i])
            for pattern in self._patterns:
                if timeframe not in pattern.timeframes:
                    continue
                signal = pattern.analyze(snapshot, store)
                if signal is None:
                    continue

                signals_count += 1
                log.info(
                    f"Backtest | {symbol} {timeframe} {signal.action} "
                    f"confidence={signal.confidence:.2f} {signal.pattern}"
                )
                pending_entry = signal
                break  # one signal per bar max

            i += 1

        # Force-close any remaining position at last candle
        if pending_entry is not None and len(candles) > 0:
            open_position = self._open_trade(
                pending_entry, candles[-1], len(candles) - 1
            )
        if open_position is not None:
            self._close_trade(
                open_position, candles[-1].close, "end_of_data", candles[-1]
            )
            trades.append(open_position)

        return trades, signals_count

    @staticmethod
    def _update_neckline_state(
        position: BacktestTrade, candle: OHLCVCandle, bar_idx: int
    ) -> None:
        """Record the first bar whose close breaks the neckline."""
        if position.neckline is None or position.neckline_break_bar_idx is not None:
            return
        if (
            position.neckline_break_direction == "below"
            and candle.close < position.neckline
        ):
            position.neckline_break_bar_idx = bar_idx
            return
        if (
            position.neckline_break_direction == "above"
            and candle.close > position.neckline
        ):
            position.neckline_break_bar_idx = bar_idx

    @staticmethod
    def _update_trailing_reference(
        position: BacktestTrade, candle: OHLCVCandle
    ) -> None:
        """Fold the just-completed bar into the trailing reference."""
        mode = position.trailing_stop_mode
        if mode == "lowest_close":
            base = position.lowest_close_since_entry
            position.lowest_close_since_entry = (
                candle.close if base is None else min(base, candle.close)
            )
        elif mode == "highest_close":
            base = position.highest_close_since_entry
            position.highest_close_since_entry = (
                candle.close if base is None else max(base, candle.close)
            )
        elif mode == "lowest_low":
            base = position.lowest_low_since_entry
            position.lowest_low_since_entry = (
                candle.low if base is None else min(base, candle.low)
            )
        elif mode == "highest_high":
            base = position.highest_high_since_entry
            position.highest_high_since_entry = (
                candle.high if base is None else max(base, candle.high)
            )

    @staticmethod
    def _trailing_stop_price(position: BacktestTrade, is_short: bool) -> float | None:
        mode = position.trailing_stop_mode
        pct = position.trailing_stop_pct
        if pct is None or mode is None:
            return None
        if is_short:
            ref = {
                "lowest_close": position.lowest_close_since_entry,
                "lowest_low": position.lowest_low_since_entry,
            }.get(mode)
            return None if ref is None else ref * (1 + pct)
        ref = {
            "highest_close": position.highest_close_since_entry,
            "highest_high": position.highest_high_since_entry,
        }.get(mode)
        return None if ref is None else ref * (1 - pct)

    @staticmethod
    def _check_exit(
        candle: OHLCVCandle, position: BacktestTrade, bar_idx: int
    ) -> tuple[float | None, str]:
        is_short = position.action == "SELL"

        # Active stop = tightest of (static stop, trailing stop). For a long
        # the tightest is the highest stop price; for a short the lowest.
        candidates: list[tuple[float, str]] = []
        if position.stop_loss is not None:
            candidates.append((position.stop_loss, "stop_loss"))
        trail = Backtester._trailing_stop_price(position, is_short)
        if trail is not None:
            candidates.append((trail, "trailing_stop"))
        if candidates:
            if is_short:
                eff, reason = min(candidates, key=lambda c: c[0])
                if candle.high >= eff:
                    return eff, reason
            else:
                eff, reason = max(candidates, key=lambda c: c[0])
                if candle.low <= eff:
                    return eff, reason

        # Take profit — direction aware.
        if position.take_profit is not None:
            if is_short:
                if candle.low <= position.take_profit:
                    return position.take_profit, "take_profit"
            else:
                if candle.high >= position.take_profit:
                    return position.take_profit, "take_profit"

        # Time exit: N bars after neckline break → exit at close.
        if (
            position.neckline_break_bar_idx is not None
            and position.exit_bars_after_neckline_break is not None
        ):
            elapsed = bar_idx - position.neckline_break_bar_idx
            if elapsed >= position.exit_bars_after_neckline_break:
                return candle.close, "time_exit"

        return None, ""

    @staticmethod
    def _open_trade(
        signal: TradeSignal, candle: OHLCVCandle, bar_idx: int
    ) -> BacktestTrade:
        position = BacktestTrade(
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            pattern=signal.pattern,
            action=signal.action,
            entry_date=candle.timestamp or datetime.now(timezone.utc),
            exit_date=candle.timestamp or datetime.now(timezone.utc),
            entry_price=candle.close,
            exit_price=candle.close,
            pnl=0.0,
            pnl_pct=0.0,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            neckline=signal.neckline,
            neckline_break_direction=signal.neckline_break_direction,
            exit_bars_after_neckline_break=signal.exit_bars_after_neckline_break,
            trailing_stop_pct=signal.trailing_stop_pct,
            trailing_stop_mode=signal.trailing_stop_mode,
            entry_bar_idx=bar_idx,
            lowest_close_since_entry=(
                candle.close if signal.trailing_stop_mode == "lowest_close" else None
            ),
            highest_close_since_entry=(
                candle.close if signal.trailing_stop_mode == "highest_close" else None
            ),
            lowest_low_since_entry=(
                candle.low if signal.trailing_stop_mode == "lowest_low" else None
            ),
            highest_high_since_entry=(
                candle.high if signal.trailing_stop_mode == "highest_high" else None
            ),
        )
        # Entry bar itself may be the neckline-break bar (entry via neckline break).
        if position.neckline is not None and position.neckline_break_bar_idx is None:
            if (
                position.neckline_break_direction == "below"
                and candle.close < position.neckline
            ):
                position.neckline_break_bar_idx = bar_idx
            elif (
                position.neckline_break_direction == "above"
                and candle.close > position.neckline
            ):
                position.neckline_break_bar_idx = bar_idx
        return position

    @staticmethod
    def _close_trade(
        position: BacktestTrade,
        exit_price: float,
        reason: str,
        candle: OHLCVCandle,
    ) -> None:
        pnl = exit_price - position.entry_price
        if position.action == "SELL":
            pnl = position.entry_price - exit_price
        pnl_pct = (pnl / position.entry_price) * 100

        position.exit_date = candle.timestamp or datetime.now(timezone.utc)
        position.exit_price = exit_price
        position.pnl = pnl
        position.pnl_pct = pnl_pct
        position.exit_reason = reason

        log.info(
            f"Backtest | EXIT {position.symbol} {position.timeframe} "
            f"reason={reason} pnl={pnl_pct:+.2f}%"
        )

    def _min_required_bars(self, timeframe: str) -> int:
        if timeframe == "1W":
            return 65
        return 120

    def _fetch_history(self, symbol: str, timeframe: str) -> list[OHLCVCandle]:
        return self._tv._fetch_history_chart(symbol, timeframe)

    @staticmethod
    def _make_snapshot(
        symbol: str, timeframe: str, candle: OHLCVCandle
    ) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=candle.timestamp or datetime.now(timezone.utc),
            candle=candle,
            indicators={
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            },
            summary={"RECOMMENDATION": "NEUTRAL"},
            oscillators={},
            moving_avgs={},
        )
