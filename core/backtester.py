"""
core/backtester.py — Historical walk-forward backtest engine.

Replays historical OHLCV data through all registered patterns bar-by-bar,
simulating entries, exits, and position management. No live data, no MCP,
no TradingView indicators — relies purely on IndicatorEngine-computed values.
"""

from __future__ import annotations

import asyncio
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
from analysis.indicator_engine import IndicatorEngine
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
    neckline: float | None = None
    neckline_break_direction: Literal["below", "above"] | None = None
    exit_bars_after_neckline_break: int | None = None
    trailing_stop_pct: float | None = None
    trailing_stop_mode: Literal["lowest_close", "highest_close"] | None = None
    trailing_activation_pct: float | None = None
    entry_bar_idx: int = -1
    neckline_break_bar_idx: int | None = None
    lowest_close_since_entry: float | None = None
    highest_close_since_entry: float | None = None
    lowest_low_since_entry: float | None = None
    highest_high_since_entry: float | None = None
    exit_reason: str = ""
    confidence: float = 0.0
    qty: float = 0.0

    # Engine-level (not pattern-level) breakeven protection. Once a trade has
    # been ahead by `breakeven_trigger_pct`, its protective floor is raised to
    # (roughly) entry price so a full round-trip back to red exits near
    # scratch instead of at the pattern's full stop distance. This is purely
    # an execution/risk-management behaviour — the pattern's own stop/target/
    # trailing values are untouched.
    breakeven_trigger_pct: float | None = None
    breakeven_buffer_pct: float = 0.0015

    _trailing_activated: bool = False
    _best_pnl_pct: float | None = None
    _breakeven_armed: bool = False

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
    initial_capital: float = 100_000.0

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
        sorted_trades = sorted(self.trades, key=lambda t: t.entry_date)
        capital = self.initial_capital
        peaks: list[float] = [capital]
        for t in sorted_trades:
            if t.qty <= 0:
                continue
            capital += t.pnl * t.qty
            peaks.append(capital)
        peak_series = np.maximum.accumulate(peaks)
        drawdowns = (np.array(peaks) - peak_series) / peak_series
        return float(drawdowns.min() * 100)

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        returns = np.array([t.pnl_pct for t in self.trades])
        mean = returns.mean()
        std = returns.std(ddof=1)
        # Guard against near-zero std (e.g. all trades have identical P&L)
        if std < 1e-10:
            return 0.0
        # Annualize using sqrt(n_trades) — each trade is an independent unit
        n = len(returns)
        return float(mean / std * np.sqrt(n))

    @property
    def account_weighted_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        sorted_trades = sorted(self.trades, key=lambda t: t.entry_date)
        capital = self.initial_capital
        for t in sorted_trades:
            if t.qty <= 0:
                continue
            pnl_dollars = t.pnl * t.qty
            capital += pnl_dollars
        return ((capital - self.initial_capital) / self.initial_capital) * 100

    @property
    def final_capital(self) -> float:
        if not self.trades:
            return self.initial_capital
        sorted_trades = sorted(self.trades, key=lambda t: t.entry_date)
        capital = self.initial_capital
        for t in sorted_trades:
            if t.qty <= 0:
                continue
            capital += t.pnl * t.qty
        return capital

    def summary(self) -> str:
        eq_pnl = self.total_pnl_pct
        aw_pnl = self.account_weighted_pnl_pct
        lines = [
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Total signals:     {self.total_signals}",
            f"  Trades taken:      {len(self.trades)}",
            f"  Wins:              {self.win_count}",
            f"  Losses:            {self.loss_count}",
            f"  Win rate:          {self.win_rate:.1%}",
            f"  Equal-weighted P&L: {eq_pnl:+.2f}%",
            f"  Account-weighted P&L: {aw_pnl:+.2f}%",
            f"  Final capital:      ${self.final_capital:,.2f}",
            f"  Avg P&L/trade:     {self.avg_pnl_pct:+.2f}%",
            f"  Max drawdown:      {self.max_drawdown_pct:+.2f}%",
            f"  Sharpe ratio:      {self.sharpe_ratio:.2f}",
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
            "equal_weighted_pnl_pct": round(self.total_pnl_pct, 4),
            "account_weighted_pnl_pct": round(self.account_weighted_pnl_pct, 4),
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
                    "confidence": round(t.confidence, 4),
                    "qty": round(t.qty, 4),
                }
                for t in self.trades
            ],
        }

    def save(self, path: str) -> None:
        p = Path(path)
        p.write_text(self.summary() + "\n", encoding="utf-8")
        log.info(f"Backtest | Results saved to {p}")


class Backtester:
    def __init__(
        self,
        symbols: list[str],
        min_confidence: float = 0.0,
        regime_filter: bool = False,
        cooldown_bars: int = 0,
        txn_cost_pct: float = 0.0,
        position_sizing: str = "pattern",
        account_value: float = 100_000.0,
        risk_per_trade_pct: float = 0.01,
        trailing_activation_default: float | None = None,
        max_open_positions: int = 8,
        min_hold_bars: int = 2,
        breakeven_trigger_pct: float | None = None,
        breakeven_buffer_pct: float = 0.0015,
        min_atr_stop_multiple: float | None = None,
        synthetic_stop_multiple: float = 1.0,
        max_loss_pct: float | None = None,
        min_reward_risk_ratio: float | None = None,
        pattern_filter: str | None = None,
    ):
        self._symbols = symbols
        self._tv = TVClient(settings.tv_screener, settings.tv_exchange)
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._pattern_filter = pattern_filter
        self._discover_patterns()

        self._min_confidence = min_confidence
        self._regime_filter = regime_filter
        self._cooldown_bars = cooldown_bars
        self._txn_cost_pct = txn_cost_pct
        self._position_sizing = position_sizing
        self._account_value = account_value
        self._risk_per_trade_pct = risk_per_trade_pct
        self._trailing_activation_default = trailing_activation_default
        self._max_open_positions = max_open_positions
        self._min_hold_bars = min_hold_bars
        # ── Execution-layer, non-pattern risk controls ──────────────────────
        # These sit on top of whatever stop/target/trailing values a pattern
        # supplies; they never change a pattern's own signal logic.
        self._breakeven_trigger_pct = breakeven_trigger_pct
        self._breakeven_buffer_pct = breakeven_buffer_pct
        self._min_atr_stop_multiple = min_atr_stop_multiple
        self._synthetic_stop_multiple = synthetic_stop_multiple
        # Hard loss cap from entry (e.g. 0.05 = -5% absolute stop). When set
        # the engine guarantees a stop_loss no worse than -max_loss_pct of
        # entry price, applied ONLY when the pattern itself supplies no
        # tighter stop. Acts as catastrophic-tail backstop without
        # interfering with the pattern's normal trailing/target logic.
        self._max_loss_pct = max_loss_pct
        # Minimum reward-to-risk ratio required before a signal is accepted.
        # reward = |take_profit - entry|, risk = |entry - stop_loss|. Signals
        # whose R:R falls below this are skipped — this is an engine-level
        # entry filter that does not alter any pattern's own signal logic,
        # stop/target/trailing values, or confidence scoring.
        self._min_reward_risk_ratio = min_reward_risk_ratio

        self._cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {}

    def _discover_patterns(self) -> None:
        for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
            if module_info.name.startswith("_") or module_info.name == "base_pattern":
                continue
            module = importlib.import_module(f"patterns.{module_info.name}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BasePattern)
                    and attr is not BasePattern
                ):
                    instance = attr()
                    # Apply pattern filter: match by case-insensitive substring
                    if self._pattern_filter is not None:
                        filter_lower = self._pattern_filter.lower()
                        if filter_lower not in instance.name.lower():
                            continue
                    self._patterns.append(instance)
                    self._pattern_files[instance.name] = (
                        f"patterns/{module_info.name}.py"
                    )
                    log.info(f"Backtester | Registered pattern: {instance}")

    async def run(self) -> BacktestResult:
        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        from tqdm import tqdm

        concurrency = settings.scanner_concurrency
        sem = asyncio.Semaphore(concurrency)

        tasks = [(s, tf) for s in self._symbols for tf in sorted(all_timeframes)]
        result = BacktestResult(
            initial_capital=self._account_value,
        )

        pbar = tqdm(total=len(tasks), desc="Backtesting", unit="sym", ncols=80)

        async def _backtest_one(symbol: str, timeframe: str):
            async with sem:
                trades, signals = await asyncio.to_thread(
                    self._backtest_symbol, symbol, timeframe
                )
                pbar.update(1)
                return trades, signals

        for coro in asyncio.as_completed(
            [_backtest_one(s, tf) for s, tf in tasks]
        ):
            trades, signals = await coro
            result.trades.extend(trades)
            result.total_signals += signals

        pbar.close()
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
                    candles[i], open_position, i,
                    min_hold_bars=self._min_hold_bars,
                )
                if exit_price is not None:
                    self._close_trade(
                        open_position, exit_price, exit_reason, candles[i],
                        self._txn_cost_pct,
                    )
                    trades.append(open_position)
                    key = (open_position.symbol, open_position.pattern)
                    self._cooldown_tracker[key] = (i, open_position.pnl < 0)
                    open_position = None
                    i += 1
                    continue
                self._update_trailing_reference(open_position, candles[i])
                i += 1
                continue

            # Enter pending position at this bar
            if pending_entry is not None:
                open_position = self._open_trade(pending_entry, candles[i], i)
                open_position.breakeven_trigger_pct = self._breakeven_trigger_pct
                open_position.breakeven_buffer_pct = self._breakeven_buffer_pct
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

                # ── Confidence filter ──────────────────────────────────────
                if signal.confidence < self._min_confidence:
                    log.debug(
                        f"Backtest | {symbol} {timeframe} confidence "
                        f"{signal.confidence:.2f} < min {self._min_confidence} — skip"
                    )
                    continue

                # ── Market regime filter (SMA200 trend) ────────────────────
                # Only buy in bull markets (price above 200MA), only short in
                # bear markets (price below 200MA). Removed SMA50 filter — it
                # was too restrictive and killed trade count.
                if self._regime_filter:
                    df = store.get_df(symbol, timeframe, min_bars=1)
                    if df is not None and len(df) >= 200:
                        close = df["close"]
                        sma200 = close.rolling(200).mean()
                        current_sma200 = float(sma200.iloc[-1])
                        current_close = float(close.iloc[-1])
                        if signal.action == "BUY" and current_close < current_sma200:
                            log.debug(
                                f"Backtest | {symbol} {timeframe} BUY below SMA200 — skip"
                            )
                            continue
                        if signal.action == "SELL" and current_close > current_sma200:
                            log.debug(
                                f"Backtest | {symbol} {timeframe} SELL above SMA200 — skip"
                            )
                            continue

                # ── Pattern cooldown ───────────────────────────────────────
                if self._cooldown_bars > 0:
                    key = (symbol, signal.pattern)
                    if key in self._cooldown_tracker:
                        exit_bar, was_loss = self._cooldown_tracker[key]
                        bars_since = i - exit_bar
                        if was_loss and bars_since < self._cooldown_bars:
                            log.debug(
                                f"Backtest | {symbol} {timeframe} cooldown "
                                f"{bars_since}/{self._cooldown_bars} bars — skip"
                            )
                            continue

                # ── Volatility quality filter (ATR vs. trailing distance) ────
                # A pattern's trailing_stop_pct is a fixed cushion (e.g. 3%).
                # On a symbol whose normal daily range is close to or larger
                # than that cushion, the stop is just noise — it will clip
                # the trade on ordinary volatility rather than a real thesis
                # failure. Require the trailing distance to be a comfortable
                # multiple of the recent ATR before taking the trade. This is
                # an engine-level entry filter; it does not alter any
                # pattern's own stop/target/trailing values.
                if (
                    self._min_atr_stop_multiple is not None
                    and signal.trailing_stop_pct is not None
                ):
                    df = store.get_df(symbol, timeframe, min_bars=1)
                    if df is not None and len(df) >= 15:
                        ind = IndicatorEngine(df)
                        atr_val = float(ind.atr(14).iloc[-1])
                        current_close = float(df["close"].iloc[-1])
                        if current_close > 0 and atr_val > 0:
                            atr_pct = atr_val / current_close
                            min_required_trail = atr_pct * self._min_atr_stop_multiple
                            if signal.trailing_stop_pct < min_required_trail:
                                log.debug(
                                    f"Backtest | {symbol} {timeframe} trailing "
                                    f"{signal.trailing_stop_pct:.2%} too thin vs "
                                    f"ATR {atr_pct:.2%} — skip"
                                )
                                continue

                # ── Synthetic stop loss (catastrophic gap protection) ────────
                # Only set when pattern provides no explicit stop_loss.
                # Distance = synthetic_stop_multiple x trailing_stop_pct.
                # At the default 1.0x this is identical to the trailing
                # distance, which means ordinary entry-day noise trips the
                # "catastrophic" stop before the trailing stop (which only
                # ratchets in from a new high/low and respects
                # min_hold_bars) ever gets a chance to manage the trade.
                # Widening this multiple keeps the hard stop as a true
                # gap/disaster backstop while letting the pattern's own
                # trailing/target logic do the day-to-day risk management.
                # Setting it to 0 disables the synthetic stop, leaving the
                # pattern's trailing/breakeven/target logic as the sole
                # exit-management layer (no catastrophic-gap backstop).
                if (
                    self._synthetic_stop_multiple > 0
                    and signal.stop_loss is None
                    and signal.trailing_stop_pct is not None
                ):
                    stop_pct = signal.trailing_stop_pct * self._synthetic_stop_multiple
                    if signal.action == "BUY":
                        signal.stop_loss = round(
                            signal.price * (1 - stop_pct), 4
                        )
                    elif signal.action == "SELL":
                        signal.stop_loss = round(
                            signal.price * (1 + stop_pct), 4
                        )

                # ── Max-loss cap (absolute floor on entry-bar+down move) ────
                # If the existing stop is wider (or absent), tighten it to
                # -max_loss_pct of the entry price. Acts as a true catastrophic
                # tail backstop without competing with the pattern's normal
                # trailing/breakeven/target logic for routine day-to-day
                # management. None (default) means no cap.
                if self._max_loss_pct is not None and self._max_loss_pct > 0:
                    if signal.action == "BUY":
                        cap_price = signal.price * (1 - self._max_loss_pct)
                        if signal.stop_loss is None or signal.stop_loss < cap_price:
                            signal.stop_loss = round(cap_price, 4)
                    elif signal.action == "SELL":
                        cap_price = signal.price * (1 + self._max_loss_pct)
                        if signal.stop_loss is None or signal.stop_loss > cap_price:
                            signal.stop_loss = round(cap_price, 4)

                # ── Reward-to-risk ratio filter ───────────────────────────
                # Skips signals whose take_profit/stop_loss ratio is below
                # the minimum. This screens out low-quality setups (e.g.
                # double_bottom/double_top where the neckline is close and
                # the synthetic stop is far) without touching the pattern's
                # own logic. Only applied when both take_profit and stop_loss
                # are present so patterns that use trailing-only exits are
                # unaffected.
                if (
                    self._min_reward_risk_ratio is not None
                    and signal.take_profit is not None
                    and signal.stop_loss is not None
                    and signal.price > 0
                ):
                    reward = abs(signal.take_profit - signal.price)
                    risk = abs(signal.price - signal.stop_loss)
                    if risk > 0 and reward / risk < self._min_reward_risk_ratio:
                        log.debug(
                            f"Backtest | {symbol} {timeframe} R:R "
                            f"{reward / risk:.2f} < min "
                            f"{self._min_reward_risk_ratio:.2f} — skip"
                        )
                        continue

                # ── Position sizing ────────────────────────────────────────
                self._apply_sizing(signal, store, symbol, timeframe)

                # ── Trailing activation default ────────────────────────────
                if (
                    self._trailing_activation_default is not None
                    and signal.trailing_activation_pct is None
                    and signal.trailing_stop_pct is not None
                ):
                    signal.trailing_activation_pct = self._trailing_activation_default

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
                open_position, candles[-1].close, "end_of_data", candles[-1],
                self._txn_cost_pct,
            )
            trades.append(open_position)

        return trades, signals_count

    # ── Sizing ──────────────────────────────────────────────────────────────────
    def _apply_sizing(
        self,
        signal: TradeSignal,
        store: OHLCVStore,
        symbol: str,
        timeframe: str,
        entry_price: float | None = None,
    ) -> None:
        current_price = entry_price if entry_price else signal.price
        if current_price <= 0:
            return

        notional_max = self._account_value * 0.02
        notional_max_shares = int(notional_max / current_price) if current_price > 0 else 0

        if self._position_sizing == "pattern":
            capped = min(signal.qty, notional_max_shares)
            signal.qty = max(1, int(capped))
            return

        if self._position_sizing == "notional":
            signal.qty = max(1, notional_max_shares)
            return

        risk_amount = self._account_value * self._risk_per_trade_pct

        if self._position_sizing == "risk":
            stop_distance = None
            if signal.stop_loss is not None:
                stop_distance = abs(current_price - signal.stop_loss)
            elif signal.trailing_stop_pct is not None:
                stop_distance = current_price * signal.trailing_stop_pct
            if stop_distance is not None and stop_distance > 0:
                qty = int(risk_amount / stop_distance)
                qty = min(qty, notional_max_shares)
                signal.qty = max(1, int(qty))
            else:
                signal.qty = max(1, notional_max_shares)
            return

        if self._position_sizing == "atr":
            df = store.get_df(symbol, timeframe, min_bars=1)
            if df is not None and len(df) >= 14:
                ind = IndicatorEngine(df)
                atr_val = float(ind.atr(14).iloc[-1])
                if atr_val > 0:
                    qty = int(risk_amount / atr_val)
                    qty = min(qty, notional_max_shares)
                    signal.qty = max(1, int(qty))
                    return
            signal.qty = max(1, notional_max_shares)
            return

    # ── Exit helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _update_neckline_state(
        position: BacktestTrade, candle: OHLCVCandle, bar_idx: int
    ) -> None:
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

        # Track best unrealized close-to-close P&L for the trailing-activation
        # and breakeven thresholds. This is tracked unconditionally (not just
        # for the "*_close" trailing modes) so activation/breakeven behave
        # consistently regardless of which trailing reference a given pattern
        # uses for its own stop distance.
        entry = position.entry_price
        if entry <= 0:
            return
        if position.action == "SELL":
            base = position.lowest_close_since_entry
            ref = candle.close if base is None else min(base, candle.close)
            position.lowest_close_since_entry = ref
            pnl = (entry - ref) / entry
        else:
            base = position.highest_close_since_entry
            ref = candle.close if base is None else max(base, candle.close)
            position.highest_close_since_entry = ref
            pnl = (ref - entry) / entry
        if position._best_pnl_pct is None or pnl > position._best_pnl_pct:
            position._best_pnl_pct = pnl

    @staticmethod
    def _trailing_stop_price(position: BacktestTrade, is_short: bool) -> float | None:
        # Check trailing activation threshold.
        # If no activation threshold is set, trailing stop is active from the start.
        if position.trailing_activation_pct is not None and not position._trailing_activated:
            if (
                position._best_pnl_pct is not None
                and position._best_pnl_pct >= position.trailing_activation_pct
            ):
                position._trailing_activated = True
            else:
                return None  # trailing stop not yet active

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
        candle: OHLCVCandle, position: BacktestTrade, bar_idx: int,
        min_hold_bars: int = 0,
    ) -> tuple[float | None, str]:
        is_short = position.action == "SELL"

        # Enforce minimum holding period before trailing stop can fire.
        # Static stop-loss and take-profit still work immediately.
        bars_held = bar_idx - position.entry_bar_idx

        candidates: list[tuple[float, str]] = []
        if position.stop_loss is not None:
            candidates.append((position.stop_loss, "stop_loss"))
        trail = None
        if bars_held >= min_hold_bars:
            trail = Backtester._trailing_stop_price(position, is_short)
        if trail is not None:
            candidates.append((trail, "trailing_stop"))

        # ── Engine-level breakeven floor ────────────────────────────────────
        # Once a trade has been ahead by breakeven_trigger_pct at some point,
        # arm a protective level at ~entry price. This only ever tightens the
        # exit (it competes with stop_loss/trailing via min/max below) — it
        # never loosens the pattern's own risk management, and it never
        # fires before min_hold_bars. A round trip back through entry then
        # exits near scratch instead of at the pattern's full stop distance.
        if (
            bars_held >= min_hold_bars
            and position.breakeven_trigger_pct is not None
            and position._best_pnl_pct is not None
        ):
            if position._best_pnl_pct >= position.breakeven_trigger_pct:
                position._breakeven_armed = True
            if position._breakeven_armed:
                buf = position.breakeven_buffer_pct
                breakeven_price = (
                    position.entry_price * (1 + buf)
                    if not is_short
                    else position.entry_price * (1 - buf)
                )
                candidates.append((breakeven_price, "breakeven_stop"))

        if candidates:
            if is_short:
                eff, reason = min(candidates, key=lambda c: c[0])
                if candle.high >= eff:
                    return eff, reason
            else:
                eff, reason = max(candidates, key=lambda c: c[0])
                if candle.low <= eff:
                    return eff, reason

        if position.take_profit is not None:
            if is_short:
                if candle.low <= position.take_profit:
                    return position.take_profit, "take_profit"
            else:
                if candle.high >= position.take_profit:
                    return position.take_profit, "take_profit"

        if (
            position.neckline_break_bar_idx is not None
            and position.exit_bars_after_neckline_break is not None
        ):
            elapsed = bar_idx - position.neckline_break_bar_idx
            if elapsed >= position.exit_bars_after_neckline_break:
                return candle.close, "time_exit"

        return None, ""

    # ── Trade lifecycle ─────────────────────────────────────────────────────────
    @staticmethod
    def _open_trade(
        signal: TradeSignal, candle: OHLCVCandle, bar_idx: int
    ) -> BacktestTrade:
        entry_price = candle.close
        # Recalculate stop_loss based on actual entry price to avoid gap distortion.
        stop_loss = signal.stop_loss
        if stop_loss is not None and signal.price > 0 and entry_price > 0:
            if signal.action == "BUY":
                pct_below = (signal.price - stop_loss) / signal.price
                stop_loss = round(entry_price * (1 - pct_below), 4)
            else:
                pct_above = (stop_loss - signal.price) / signal.price
                stop_loss = round(entry_price * (1 + pct_above), 4)
        # Recalculate take_profit similarly.
        take_profit = signal.take_profit
        if take_profit is not None and signal.price > 0 and entry_price > 0:
            if signal.action == "BUY":
                pct_above = (take_profit - signal.price) / signal.price
                take_profit = round(entry_price * (1 + pct_above), 4)
            else:
                pct_below = (signal.price - take_profit) / signal.price
                take_profit = round(entry_price * (1 - pct_below), 4)
        position = BacktestTrade(
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            pattern=signal.pattern,
            action=signal.action,
            entry_date=candle.timestamp or datetime.now(timezone.utc),
            exit_date=candle.timestamp or datetime.now(timezone.utc),
            entry_price=entry_price,
            exit_price=entry_price,
            pnl=0.0,
            pnl_pct=0.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            neckline=signal.neckline,
            neckline_break_direction=signal.neckline_break_direction,
            exit_bars_after_neckline_break=signal.exit_bars_after_neckline_break,
            trailing_stop_pct=signal.trailing_stop_pct,
            trailing_stop_mode=signal.trailing_stop_mode,
            trailing_activation_pct=signal.trailing_activation_pct,
            entry_bar_idx=bar_idx,
            confidence=signal.confidence,
            qty=signal.qty,
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
        txn_cost_pct: float = 0.0,
    ) -> None:
        pnl = exit_price - position.entry_price
        if position.action == "SELL":
            pnl = position.entry_price - exit_price

        cost = position.entry_price * txn_cost_pct + exit_price * txn_cost_pct
        pnl -= cost
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
