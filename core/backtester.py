"""
core/backtester.py — Historical walk-forward backtest engine.

Replays historical OHLCV data through all registered patterns bar-by-bar,
simulating entries, exits, and position management. No live data, no MCP,
no TradingView indicators — relies purely on IndicatorEngine-computed values.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
import importlib
import json
import multiprocessing as mp
import os
import pkgutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import pandas as pd

import patterns as patterns_pkg
from config import settings
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.tv_client import TVClient, MarketSnapshot, OHLCVCandle, SCREENER_FIELDS
from patterns.base_pattern import BasePattern, TradeSignal, skip_pattern_module
from analysis.indicator_engine import IndicatorEngine
from analysis.price_volume import compute_volume_metrics, volume_confirm_gate
from core.engine_defaults import (
    ENGINE,
    passes_cooldown,
    passes_min_confidence,
    passes_min_share_price,
    passes_regime_filter,
)
from core.kronos_gate import kronos_gate_check
from core.market import apply_lot_rounding, get_market, ohlcv_cache_key
from utils.logger import log

# ── OHLCV disk cache ──────────────────────────────────────────────────────────
_CACHE_DIR = Path("data/cache")
_CACHE_TTL_SECONDS = 6 * 3600  # re-fetch if cache older than 6 hours


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
    time_exit_only_unfavorable: bool = False
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
    notes: str = ""
    # Price-volume metrics at signal time (analysis.price_volume).
    rvol: float | None = None
    obv_slope: float | None = None

    # Paper trading overwrites entry_date/exit_date with the real wall-clock
    # fill time (see core.paper_trader) so the dashboard can show "when did
    # this actually happen" even under a compressed stream replay. These two
    # keep the underlying bar's own timestamp so days_held() can still report
    # simulated holding time instead of real elapsed minutes. Unused by the
    # backtester itself, where entry_date/exit_date already are bar time.
    sim_entry_date: datetime | None = None
    sim_exit_date: datetime | None = None

    # Execution diagnostics. These are populated when an exit is evaluated so
    # paper/backtest runs can explain exactly how many strategy bars elapsed.
    # A deferred next-bar fill can have fewer position-held bars than the
    # pattern's neckline time-stop elapsed bars.
    exit_bar_idx: int | None = None
    time_exit_bars_elapsed: int | None = None

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
    # Mark-to-market portfolio equity by session date. This is populated after
    # the shared capital ledger is applied, so drawdown/Sharpe reflect actual
    # concurrent positions rather than realized trade P&L alone.
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    initial_capital: float = 100_000.0
    # Signals that had an otherwise-valid entry but were dropped by the
    # capital ledger (see _apply_capital_ledger) because the account was
    # already fully deployed in other open positions at that moment.
    capital_rejected: int = 0

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
    def avg_win_pct(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def largest_win_pct(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl > 0]
        return max(wins) if wins else 0.0

    @property
    def largest_loss_pct(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl < 0]
        return min(losses) if losses else 0.0

    @property
    def avg_r(self) -> float:
        values = [
            r for t in self.trades
            if (r := trade_r_multiple(t, t.exit_price)) is not None
        ]
        return float(np.mean(values)) if values else 0.0

    @property
    def median_r(self) -> float:
        values = [
            r for t in self.trades
            if (r := trade_r_multiple(t, t.exit_price)) is not None
        ]
        return float(np.median(values)) if values else 0.0

    @property
    def exit_reason_breakdown(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for t in self.trades:
            counts[t.exit_reason or "unknown"] += 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def avg_hold_bars(self) -> float:
        values = [
            t.exit_bar_idx - t.entry_bar_idx
            for t in self.trades
            if t.exit_bar_idx is not None and t.entry_bar_idx >= 0
        ]
        return float(np.mean(values)) if values else 0.0

    @property
    def profit_factor(self) -> float:
        # t.pnl is a per-share $ diff — weight by qty so this reflects actual
        # position-sized dollar P&L, not raw share-price magnitude.
        gross_profit = sum(t.pnl * t.qty for t in self.trades if t.pnl > 0)
        gross_loss = -sum(t.pnl * t.qty for t in self.trades if t.pnl < 0)
        if gross_loss < 1e-10:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def expectancy_pct(self) -> float:
        """Average P&L% per trade, weighted by win rate — the number that
        answers "is this strategy worth trading" better than win rate alone
        (a 30%-win-rate strategy can still be very profitable)."""
        if not self.trades:
            return 0.0
        return self.win_rate * self.avg_win_pct + (1 - self.win_rate) * self.avg_loss_pct

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve:
            values = np.asarray([v for _, v in self.equity_curve], dtype=float)
            peaks = np.maximum.accumulate(values)
            valid = peaks > 0
            if valid.any():
                return float(np.min((values[valid] - peaks[valid]) / peaks[valid]) * 100)
        return _drawdown_pct(self.trades, self.initial_capital)

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe from mark-to-market daily portfolio equity."""
        if len(self.equity_curve) < 2:
            return 0.0
        values = np.asarray([v for _, v in self.equity_curve], dtype=float)
        prev = values[:-1]
        returns = np.divide(
            values[1:] - prev, prev,
            out=np.zeros_like(values[1:]),
            where=np.abs(prev) > 1e-12,
        )
        if len(returns) < 2:
            return 0.0
        std = returns.std(ddof=1)
        if std < 1e-10:
            return 0.0
        return float(returns.mean() / std * np.sqrt(252))

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

    def by_pattern(self) -> dict[str, list[BacktestTrade]]:
        groups: dict[str, list[BacktestTrade]] = defaultdict(list)
        for t in self.trades:
            groups[t.pattern].append(t)
        return groups

    def pattern_breakdown(self) -> dict[str, dict]:
        """Per-pattern stats (trades, win_rate, total/avg pnl%, profit_factor,
        avg R, avg hold time, max drawdown), ranked best-to-worst by total
        P&L% — e.g. for a pattern leaderboard."""
        groups = self.by_pattern()
        ranked = sorted(
            groups.items(),
            key=lambda kv: self._pattern_stats(kv[1], self.initial_capital)["total_pnl_pct"],
            reverse=True,
        )
        return {
            pattern: self._pattern_stats(trades, self.initial_capital)
            for pattern, trades in ranked
        }

    @staticmethod
    def _pattern_stats(trades: list[BacktestTrade], initial_capital: float = 100_000.0) -> dict:
        wins = sum(1 for t in trades if t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl < 0)
        total_pnl_pct = sum(t.pnl_pct for t in trades)
        n = len(trades)
        gross_profit = sum(t.pnl * t.qty for t in trades if t.pnl > 0)
        gross_loss = -sum(t.pnl * t.qty for t in trades if t.pnl < 0)
        if gross_loss < 1e-10:
            profit_factor = float("inf") if gross_profit > 0 else 0.0
        else:
            profit_factor = gross_profit / gross_loss
        r_values = [
            r for t in trades
            if (r := trade_r_multiple(t, t.exit_price)) is not None
        ]
        hold_days = [
            (t.exit_date - t.entry_date).total_seconds() / 86400 for t in trades
        ]
        return {
            "trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "total_pnl_pct": round(total_pnl_pct, 4),
            "avg_pnl_pct": round(total_pnl_pct / n, 4) if n else 0.0,
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "avg_r": round(sum(r_values) / len(r_values), 2) if r_values else None,
            "avg_hold_days": round(sum(hold_days) / len(hold_days), 2) if hold_days else 0.0,
            "max_drawdown_pct": round(_drawdown_pct(trades, initial_capital), 2),
        }

    def summary(self) -> str:
        eq_pnl = self.total_pnl_pct
        aw_pnl = self.account_weighted_pnl_pct
        lines = [
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Total signals:     {self.total_signals}",
            f"  Trades taken:      {len(self.trades)}",
            f"  Rejected (no cash): {self.capital_rejected}",
            f"  Wins:              {self.win_count}",
            f"  Losses:            {self.loss_count}",
            f"  Win rate:          {self.win_rate:.1%}",
            # eq_pnl sums each trade's independent % return as if it alone got
            # 100% of capital — not a portfolio return, just a per-trade-edge
            # diagnostic. aw_pnl (real position sizes, real $ P&L / capital) is
            # the actual return this strategy would have produced.
            f"  Equal-weighted P&L (sum of per-trade %, not a real return): {eq_pnl:+.2f}%",
            f"  Account-weighted P&L (real sizing, actual return): {aw_pnl:+.2f}%",
            f"  Final capital:      ${self.final_capital:,.2f}",
            f"  Avg P&L/trade:     {self.avg_pnl_pct:+.2f}%",
            f"  Avg winner:        {self.avg_win_pct:+.2f}%",
            f"  Avg loser:         {self.avg_loss_pct:+.2f}%",
            f"  Largest win:       {self.largest_win_pct:+.2f}%",
            f"  Largest loss:      {self.largest_loss_pct:+.2f}%",
            (
                f"  Profit factor:     {self.profit_factor:.2f}"
                if self.profit_factor != float("inf")
                else "  Profit factor:     inf (no losers)"
            ),
            f"  Expectancy/trade:  {self.expectancy_pct:+.2f}%",
            f"  Avg R:             {self.avg_r:+.2f}",
            f"  Median R:          {self.median_r:+.2f}",
            f"  Avg hold:          {self.avg_hold_bars:.1f} bars",
            f"  Max drawdown:      {self.max_drawdown_pct:+.2f}%",
            f"  Sharpe ratio:      {self.sharpe_ratio:.2f}",
            "=" * 60,
        ]
        breakdown = self.pattern_breakdown()
        if len(breakdown) > 1:
            lines.append("  BY PATTERN")
            lines.append("-" * 60)
            for pattern, s in breakdown.items():
                pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "inf"
                avg_r = f"{s['avg_r']:+.2f}" if s["avg_r"] is not None else "-"
                lines.append(
                    f"  {pattern:35s} n={s['trades']:<4d} "
                    f"win={s['win_rate']:.0%} "
                    f"pnl={s['total_pnl_pct']:+.2f}% "
                    f"avg={s['avg_pnl_pct']:+.2f}% "
                    f"pf={pf} avgR={avg_r} "
                    f"hold={s['avg_hold_days']:.1f}d "
                    f"maxDD={s['max_drawdown_pct']:+.2f}%"
                )
            lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_signals": self.total_signals,
            "trades_taken": len(self.trades),
            "capital_rejected": self.capital_rejected,
            "wins": self.win_count,
            "losses": self.loss_count,
            "win_rate": round(self.win_rate, 4),
            "equal_weighted_pnl_pct": round(self.total_pnl_pct, 4),
            "account_weighted_pnl_pct": round(self.account_weighted_pnl_pct, 4),
            "avg_pnl_pct": round(self.avg_pnl_pct, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "largest_win_pct": round(self.largest_win_pct, 4),
            "largest_loss_pct": round(self.largest_loss_pct, 4),
            "profit_factor": (
                round(self.profit_factor, 4)
                if self.profit_factor != float("inf")
                else None
            ),
            "expectancy_pct": round(self.expectancy_pct, 4),
            "avg_r": round(self.avg_r, 4),
            "median_r": round(self.median_r, 4),
            "avg_hold_bars": round(self.avg_hold_bars, 2),
            "exit_reason_breakdown": dict(self.exit_reason_breakdown),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "equity_curve": [
                {"date": ts, "equity": round(value, 4)}
                for ts, value in self.equity_curve
            ],
            "by_pattern": self.pattern_breakdown(),
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
                    "notes": t.notes,
                    "rvol": round(t.rvol, 4) if t.rvol is not None else None,
                    "obv_slope": round(t.obv_slope, 4) if t.obv_slope is not None else None,
                }
                for t in self.trades
            ],
        }

    def save(self, path: str) -> None:
        p = Path(path)
        lines = [self.summary(), "", "  TRADES", "-" * 60]
        for t in sorted(self.trades, key=lambda t: t.entry_date):
            lines.append(
                f"  {t.entry_date.strftime('%Y-%m-%d')} {t.action:5s} {t.symbol:8s} "
                f"{t.timeframe:4s} entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                f"pnl={t.pnl_pct:+.2f}% reason={t.exit_reason} pattern={t.pattern}"
            )
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info(f"Backtest | Results saved to {p}")


# ── Trade math shared by reporting (backtest + paper trading) ────────────────
# Pure functions, not BacktestTrade methods, so they work identically on an
# open position (pass the current price) or a closed trade (pass exit_price).

def trade_r_multiple(trade: BacktestTrade, price: float) -> float | None:
    """Gain/loss expressed in multiples of the initial stop distance ("R") —
    e.g. +2R means the trade is up 2x what it was risking. None if the trade
    has no stop_loss to measure risk against."""
    if trade.stop_loss is None or trade.entry_price <= 0:
        return None
    risk = abs(trade.entry_price - trade.stop_loss)
    if risk <= 0:
        return None
    move = price - trade.entry_price
    if trade.action == "SELL":
        move = -move
    return move / risk


def trade_risk_dollars(trade: BacktestTrade) -> float | None:
    if trade.stop_loss is None:
        return None
    return abs(trade.entry_price - trade.stop_loss) * trade.qty


def _realized_daily_returns(
    trades: list[BacktestTrade], initial_capital: float,
) -> np.ndarray:
    """Build daily returns from realized P&L only.

    This intentionally does not claim to be mark-to-market portfolio Sharpe:
    open-position equity is unavailable at this reporting layer. Keeping the
    calculation daily and explicitly realized avoids the old per-trade
    sqrt(n_trades) annualization error.
    """
    if not trades or initial_capital <= 0:
        return np.array([], dtype=float)
    pnl_by_day: dict[datetime.date, float] = defaultdict(float)
    for trade in trades:
        if trade.qty <= 0:
            continue
        pnl_by_day[trade.exit_date.date()] += trade.pnl * trade.qty
    capital = float(initial_capital)
    returns: list[float] = []
    for day in sorted(pnl_by_day):
        if capital <= 0:
            break
        pnl = pnl_by_day[day]
        returns.append(pnl / capital)
        capital += pnl
    return np.asarray(returns, dtype=float)


def _drawdown_pct(trades: list[BacktestTrade], initial_capital: float) -> float:
    if not trades:
        return 0.0
    sorted_trades = sorted(trades, key=lambda t: t.entry_date)
    capital = initial_capital
    peaks: list[float] = [capital]
    for t in sorted_trades:
        if t.qty <= 0:
            continue
        capital += t.pnl * t.qty
        peaks.append(capital)
    peak_series = np.maximum.accumulate(peaks)
    drawdowns = (np.array(peaks) - peak_series) / peak_series
    return float(drawdowns.min() * 100)


def _build_portfolio_equity_curve(
    trades: list[BacktestTrade],
    ohlcv_data: dict[tuple[str, str], list[OHLCVCandle]],
    initial_capital: float,
    txn_cost_pct: float,
) -> list[tuple[str, float]]:
    """Mark the post-ledger portfolio to market once per trading/session date.

    Trades are generated independently per symbol for parallelism, then the
    shared capital ledger assigns final quantities. Replaying those accepted
    trades here with the original OHLCV lets reporting account for concurrent
    long market value, short liabilities, entry/exit fees, and unrealized P&L.
    """
    if not trades or initial_capital <= 0:
        return []

    start = min(t.entry_date.date() for t in trades)
    end = max(t.exit_date.date() for t in trades)
    dates: set = set()
    for candles in ohlcv_data.values():
        for candle in candles:
            if candle.timestamp is None:
                continue
            d = candle.timestamp.date()
            if start <= d <= end:
                dates.add(d)
    dates.update(t.entry_date.date() for t in trades)
    dates.update(t.exit_date.date() for t in trades)
    if not dates:
        return []

    # Fast lookup of the latest available mark at or before each date.
    mark_series: dict[tuple[str, str], list[tuple[object, float]]] = {}
    for key, candles in ohlcv_data.items():
        rows = [
            (c.timestamp.date(), c.close)
            for c in candles
            if c.timestamp is not None and start <= c.timestamp.date() <= end
        ]
        if rows:
            mark_series[key] = rows

    def mark_for(trade: BacktestTrade, day):
        rows = mark_series.get((trade.symbol, trade.timeframe), [])
        if not rows:
            return trade.entry_price
        dates_only = [d for d, _ in rows]
        idx = bisect_right(dates_only, day) - 1
        return rows[idx][1] if idx >= 0 else trade.entry_price

    by_entry: dict = defaultdict(list)
    by_exit: dict = defaultdict(list)
    for trade in trades:
        by_entry[trade.entry_date.date()].append(trade)
        by_exit[trade.exit_date.date()].append(trade)

    cash = float(initial_capital)
    open_trades: list[BacktestTrade] = []
    curve: list[tuple[str, float]] = [
        ((start - timedelta(days=1)).isoformat(), float(initial_capital))
    ]

    for day in sorted(dates):
        # Same-day exits free capital before same-day entries, matching the
        # shared capital ledger's event ordering.
        for trade in by_exit.get(day, []):
            if trade in open_trades:
                open_trades.remove(trade)
            exit_cost = trade.exit_price * trade.qty * txn_cost_pct
            if trade.action == "BUY":
                cash += trade.exit_price * trade.qty - exit_cost
            else:
                cash -= trade.exit_price * trade.qty + exit_cost

        for trade in by_entry.get(day, []):
            if trade.qty <= 0:
                continue
            entry_cost = trade.entry_price * trade.qty * txn_cost_pct
            if trade.action == "BUY":
                cash -= trade.entry_price * trade.qty + entry_cost
            else:
                cash += trade.entry_price * trade.qty - entry_cost
            open_trades.append(trade)

        equity = cash
        for trade in open_trades:
            mark = mark_for(trade, day)
            if trade.action == "BUY":
                equity += mark * trade.qty
            else:
                equity -= mark * trade.qty
        curve.append((day.isoformat(), float(equity)))

    return curve

# ── Module-level backtest helpers ─────────────────────────────────────────────
# These are picklable functions used by both Backtester (main process) and
# _worker_symbol_backtest (subprocess via ProcessPoolExecutor). Extracting
# them from the class avoids pickling bound methods of unpicklable objects.


def _min_required_bars(timeframe: str) -> int:
    if timeframe == "1W":
        return 65
    return 120


def _make_snapshot(
    symbol: str, timeframe: str, candle: OHLCVCandle,
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


def _update_neckline_state(
    position: BacktestTrade, candle: OHLCVCandle, bar_idx: int,
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


def _update_trailing_reference(
    position: BacktestTrade, candle: OHLCVCandle,
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


def _trailing_stop_price(position: BacktestTrade, is_short: bool) -> float | None:
    if (
        position.trailing_activation_pct is not None
        and not position._trailing_activated
    ):
        if (
            position._best_pnl_pct is not None
            and position._best_pnl_pct >= position.trailing_activation_pct
        ):
            position._trailing_activated = True
        else:
            return None
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


def _gap_aware_trigger_fill(
    candle: OHLCVCandle,
    trigger: float,
    *,
    is_short: bool,
    favorable: bool,
) -> float | None:
    """Return a realistic daily-bar fill for a triggered stop/target.

    If the session opens through the trigger, the order cannot be filled at
    the stale trigger price; it is assumed filled at the open. Otherwise the
    trigger price is used once the intrabar range crosses it.
    """
    if trigger <= 0 or candle.open <= 0:
        return None

    if is_short:
        crossed = candle.low <= trigger if favorable else candle.high >= trigger
        gapped = candle.open <= trigger if favorable else candle.open >= trigger
    else:
        crossed = candle.high >= trigger if favorable else candle.low <= trigger
        gapped = candle.open >= trigger if favorable else candle.open <= trigger

    if not crossed and not gapped:
        return None
    return candle.open if gapped else trigger


def _time_exit_ready(position: BacktestTrade, candle, bar_idx: int) -> bool:
    """True when the pattern time-stop is due. Optional underwater-only gate."""
    if (
        position.neckline_break_bar_idx is None
        or position.exit_bars_after_neckline_break is None
    ):
        return False
    elapsed = bar_idx - position.neckline_break_bar_idx
    if elapsed < position.exit_bars_after_neckline_break:
        return False
    if position.time_exit_only_unfavorable:
        is_short = position.action == "SELL"
        if is_short:
            if candle.close <= position.entry_price:
                return False
        elif candle.close >= position.entry_price:
            return False
    return True


def _check_exit(
    candle: OHLCVCandle,
    position: BacktestTrade,
    bar_idx: int,
    min_hold_bars: int = 0,
) -> tuple[float | None, str]:
    is_short = position.action == "SELL"
    bars_held = bar_idx - position.entry_bar_idx
    candidates: list[tuple[float, str]] = []
    if position.stop_loss is not None:
        candidates.append((position.stop_loss, "stop_loss"))
    trail = None
    if bars_held >= min_hold_bars:
        trail = _trailing_stop_price(position, is_short)
    if trail is not None:
        candidates.append((trail, "trailing_stop"))
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
        fills = []
        for level, reason in candidates:
            fill = _gap_aware_trigger_fill(
                candle, level, is_short=is_short, favorable=False,
            )
            if fill is not None:
                fills.append((fill, reason))
        if fills:
            # A single daily bar can cross several protective levels (hard
            # stop, trailing stop, breakeven floor). Without intrabar
            # sequencing we cannot know which one actually filled first, so
            # model the worst plausible protective fill for the trade
            # direction instead of the most favourable one.
            if is_short:
                fill, reason = max(fills, key=lambda f: f[0])
            else:
                fill, reason = min(fills, key=lambda f: f[0])
            position.exit_bar_idx = bar_idx
            return fill, reason

    if position.take_profit is not None:
        fill = _gap_aware_trigger_fill(
            candle, position.take_profit, is_short=is_short, favorable=True,
        )
        if fill is not None:
            position.exit_bar_idx = bar_idx
            return fill, "take_profit"
    if _time_exit_ready(position, candle, bar_idx):
        elapsed = bar_idx - position.neckline_break_bar_idx
        position.exit_bar_idx = bar_idx
        position.time_exit_bars_elapsed = elapsed
        return candle.close, "time_exit"
    return None, ""


def describe_risk_gate_rejection(
    signal: TradeSignal,
    store: OHLCVStore,
    symbol: str,
    timeframe: str,
    *,
    min_atr_stop_multiple: float | None = None,
    synthetic_stop_multiple: float = 0.0,
    atr_stop_floor_multiple: float | None = None,
    hard_stop_percentage: float | None = None,
    min_reward_risk_ratio: float | None = None,
    trailing_activation_default: float | None = None,
) -> str | None:
    """Mutate stop backstops in place. Return a reject reason, or None if the
    setup clears the same filters the formal backtester uses."""
    if min_atr_stop_multiple is not None and signal.trailing_stop_pct is not None:
        df = store.get_df(symbol, timeframe, min_bars=1)
        if df is not None and len(df) >= 15:
            ind = IndicatorEngine(df)
            atr_val = float(ind.atr(14).iloc[-1])
            current_close = float(df["close"].iloc[-1])
            if current_close > 0 and atr_val > 0:
                atr_pct = atr_val / current_close
                min_required_trail = atr_pct * min_atr_stop_multiple
                if signal.trailing_stop_pct < min_required_trail:
                    log.info(
                        f"RiskGate | {symbol} {timeframe} widen trail "
                        f"{signal.trailing_stop_pct:.2%} → {min_required_trail:.2%} "
                        f"(1× ATR {atr_pct:.2%})"
                    )
                    signal.trailing_stop_pct = min_required_trail

    if (
        synthetic_stop_multiple > 0
        and signal.stop_loss is None
        and signal.trailing_stop_pct is not None
    ):
        stop_pct = signal.trailing_stop_pct * synthetic_stop_multiple
        if signal.action == "BUY":
            signal.stop_loss = round(signal.price * (1 - stop_pct), 4)
        elif signal.action == "SELL":
            signal.stop_loss = round(signal.price * (1 + stop_pct), 4)

    if atr_stop_floor_multiple is not None and signal.stop_loss is not None:
        df = store.get_df(symbol, timeframe, min_bars=1)
        if df is not None and len(df) >= 15:
            ind = IndicatorEngine(df)
            atr_val = float(ind.atr(14).iloc[-1])
            if atr_val > 0:
                floor_distance = atr_val * atr_stop_floor_multiple
                current_distance = abs(signal.price - signal.stop_loss)
                if current_distance < floor_distance:
                    if signal.action == "BUY":
                        signal.stop_loss = round(signal.price - floor_distance, 4)
                    elif signal.action == "SELL":
                        signal.stop_loss = round(signal.price + floor_distance, 4)

    if hard_stop_percentage is not None and hard_stop_percentage > 0:
        if signal.action == "BUY":
            cap_price = signal.price * (1 - hard_stop_percentage)
            if signal.stop_loss is None or signal.stop_loss < cap_price:
                signal.stop_loss = round(cap_price, 4)
        elif signal.action == "SELL":
            cap_price = signal.price * (1 + hard_stop_percentage)
            if signal.stop_loss is None or signal.stop_loss > cap_price:
                signal.stop_loss = round(cap_price, 4)

    if (
        min_reward_risk_ratio is not None
        and signal.take_profit is not None
        and signal.stop_loss is not None
        and signal.price > 0
    ):
        reward = abs(signal.take_profit - signal.price)
        risk = abs(signal.price - signal.stop_loss)
        if risk > 0 and reward / risk < min_reward_risk_ratio:
            log.debug(
                f"RiskGate | {symbol} {timeframe} R:R "
                f"{reward / risk:.2f} < min {min_reward_risk_ratio:.2f} — skip"
            )
            return (
                f"Risk gate: reward:risk {reward / risk:.2f} is below min "
                f"{min_reward_risk_ratio:.2f} after stop backstops (synthetic/"
                f"ATR floor/hard stop). Same filter as the formal backtester."
            )

    if (
        trailing_activation_default is not None
        and signal.trailing_activation_pct is None
        and signal.trailing_stop_pct is not None
    ):
        signal.trailing_activation_pct = trailing_activation_default

    return None


def apply_risk_gates(
    signal: TradeSignal,
    store: OHLCVStore,
    symbol: str,
    timeframe: str,
    **kwargs,
) -> bool:
    """Shared entry-gate/stop-backstop pipeline. Mutates signal; False = drop."""
    return describe_risk_gate_rejection(
        signal, store, symbol, timeframe, **kwargs,
    ) is None


def _execution_reward_risk_ok(
    position: BacktestTrade, minimum_rr: float | None,
) -> bool:
    """Validate reward/risk again using the actual simulated fill price."""
    if minimum_rr is None or minimum_rr <= 0:
        return True
    if position.stop_loss is None or position.take_profit is None:
        return False
    risk = abs(position.entry_price - position.stop_loss)
    reward = abs(position.take_profit - position.entry_price)
    if risk <= 0:
        return False
    return reward / risk >= minimum_rr


def _open_trade(
    signal: TradeSignal, candle: OHLCVCandle, bar_idx: int,
) -> BacktestTrade:
    entry_price = candle.close
    stop_loss = signal.stop_loss
    if stop_loss is not None and signal.price > 0 and entry_price > 0:
        if signal.action == "BUY":
            pct_below = (signal.price - stop_loss) / signal.price
            stop_loss = round(entry_price * (1 - pct_below), 4)
        else:
            pct_above = (stop_loss - signal.price) / signal.price
            stop_loss = round(entry_price * (1 + pct_above), 4)
    take_profit = signal.take_profit
    if take_profit is not None and signal.price > 0 and entry_price > 0:
        if signal.action == "BUY":
            pct_above = (take_profit - signal.price) / signal.price
            take_profit = round(entry_price * (1 + pct_above), 4)
        else:
            pct_below = (signal.price - take_profit) / signal.price
            take_profit = round(entry_price * (1 - pct_below), 4)
    # A pattern's target is computed off a reference level (e.g. neckline),
    # not off the actual fill price — a breakout candle can already close
    # past that target by the time the entry confirms. Rebasing then lands
    # the target on the wrong side of entry_price, which trips take_profit/
    # stop_loss on the very next tick at a loss instead of a gain. Drop an
    # invalid target rather than act on it; trailing_stop/time_exit still govern.
    if signal.action == "BUY":
        if stop_loss is not None and stop_loss >= entry_price:
            stop_loss = None
        if take_profit is not None and take_profit <= entry_price:
            take_profit = None
    else:
        if stop_loss is not None and stop_loss <= entry_price:
            stop_loss = None
        if take_profit is not None and take_profit >= entry_price:
            take_profit = None
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
        time_exit_only_unfavorable=signal.time_exit_only_unfavorable,
        trailing_stop_pct=signal.trailing_stop_pct,
        trailing_stop_mode=signal.trailing_stop_mode,
        trailing_activation_pct=signal.trailing_activation_pct,
        entry_bar_idx=bar_idx,
        confidence=signal.confidence,
        qty=signal.qty,
        notes=signal.notes,
        rvol=signal.rvol,
        obv_slope=signal.obv_slope,
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
    # The signal is generated on the breakout/confirmation bar, but this
    # engine deliberately fills on the next bar. Preserve that event across
    # the deferred entry so neckline time-stops start at the signal bar.
    if (
        position.neckline is not None
        and position.neckline_break_bar_idx is None
        and signal.signal_bar_idx is not None
        and position.exit_bars_after_neckline_break is not None
        and position.neckline_break_direction is not None
    ):
        position.neckline_break_bar_idx = signal.signal_bar_idx
    elif position.neckline is not None and position.neckline_break_bar_idx is None:
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


def _apply_sizing(
    signal: TradeSignal,
    store: OHLCVStore,
    symbol: str,
    timeframe: str,
    account_value: float,
    risk_per_trade_pct: float,
    position_sizing: str,
    entry_price: float | None = None,
    max_position_pct: float = 0.02,
) -> None:
    current_price = entry_price if entry_price else signal.price
    if current_price <= 0:
        return
    # Diversification ceiling — the largest fraction of account_value any
    # single position may occupy, regardless of sizing mode. Kept separate
    # from risk_per_trade_pct: if this is tighter than what risk_per_trade_pct
    # implies for a given stop distance, it silently caps every trade to the
    # same size and risk_per_trade_pct stops mattering.
    notional_max = account_value * max_position_pct
    notional_max_shares = (
        int(notional_max / current_price) if current_price > 0 else 0
    )
    if position_sizing == "pattern":
        capped = min(signal.qty, notional_max_shares)
        signal.qty = max(1, int(capped))
        return
    if position_sizing == "notional":
        signal.qty = max(1, notional_max_shares)
        return
    risk_amount = account_value * risk_per_trade_pct
    if position_sizing == "risk":
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
    if position_sizing == "atr":
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


def _load_patterns(pattern_specs: list[tuple[str, str]]) -> list[BasePattern]:
    patterns: list[BasePattern] = []
    for module_name, class_name in pattern_specs:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        patterns.append(cls())
    return patterns


def _iter_pattern_classes() -> list[tuple[str, type[BasePattern]]]:
    """Yield (module_name, class) for every BasePattern subclass in patterns/."""
    found: list[tuple[str, type[BasePattern]]] = []
    for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
        if skip_pattern_module(module_info.name):
            continue
        module = importlib.import_module(f"patterns.{module_info.name}")
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePattern)
                and attr is not BasePattern
            ):
                found.append((module_info.name, attr))
    return found


def discover_pattern_names() -> list[str]:
    """All registered pattern names, e.g. for populating a UI filter dropdown."""
    return sorted({
        inst.name for _, cls in _iter_pattern_classes()
        if not (inst := cls()).skipped
    })


def _enforce_max_open_positions(
    trades: list[BacktestTrade],
    max_open_positions: int,
    max_open_per_pattern: int = 0,
) -> list[BacktestTrade]:
    """Cap portfolio-wide concurrent positions across all symbols/patterns.

    Each symbol is backtested independently (for parallelism), so this
    cap can't be enforced during simulation. Instead, replay accepted
    trades in chronological order and reject any entry that would push
    the concurrently-open count above the cap — highest-confidence
    entries win ties on the same bar.
    """
    if max_open_positions <= 0 and max_open_per_pattern <= 0:
        return trades
    ordered = sorted(trades, key=lambda t: (t.entry_date, -t.confidence))
    accepted: list[BacktestTrade] = []
    open_exits: list[datetime] = []
    open_by_pattern: dict[str, list[datetime]] = {}
    for t in ordered:
        open_exits = [d for d in open_exits if d > t.entry_date]
        pat_open = [d for d in open_by_pattern.get(t.pattern, []) if d > t.entry_date]
        if max_open_positions > 0 and len(open_exits) >= max_open_positions:
            continue
        if max_open_per_pattern > 0 and len(pat_open) >= max_open_per_pattern:
            continue
        open_exits.append(t.exit_date)
        open_by_pattern.setdefault(t.pattern, []).append(t.exit_date)
        accepted.append(t)
    return accepted


def _apply_capital_ledger(
    trades: list[BacktestTrade],
    initial_capital: float,
    risk_per_trade_pct: float,
    position_sizing: str,
    max_position_pct: float,
    max_gross_exposure_pct: float = 0.0,
    txn_cost_pct: float = 0.0,
) -> tuple[list[BacktestTrade], int]:
    """Re-size every trade against one shared cash ledger, replayed in
    chronological order, instead of each trade being sized independently
    against a fixed initial account_value.

    Each symbol is backtested independently (for parallelism), so neither
    of these can be enforced during simulation — only here, as a
    post-processing pass over the merged trade list:
      1. Compounding: qty is recomputed off *current* ledger cash, not the
         static initial account_value, so winners size up later trades.
      2. A real capital constraint: concurrently open positions can never
         collectively commit more cash than the account actually has.
         A signal that arrives while the account is fully deployed
         elsewhere is dropped, same as a broker rejecting an order for
         insufficient buying power — this is a cash account model, no
         margin/leverage.
    """
    if not trades:
        return trades, 0

    # Exits before entries at the same timestamp, so same-day exits free
    # cash for same-day entries instead of falsely starving them.
    events = sorted(
        [(t.exit_date, 0, t) for t in trades] + [(t.entry_date, 1, t) for t in trades],
        key=lambda e: (e[0], e[1]),
    )

    cash = initial_capital
    accepted: list[BacktestTrade] = []
    opened: set[int] = set()
    rejected = 0
    long_notional = 0.0
    short_notional = 0.0
    notional_by_id: dict[int, float] = {}
    # Gross exposure is an equity percentage, so the cap compounds with the
    # account instead of remaining frozen at the initial balance.
    gross_cap = 0.0

    def equity() -> float:
        # At an entry/exit event the open positions are marked at their
        # entry/exit prices. Short-sale proceeds are liabilities, not free
        # equity, so they are excluded from equity via short_notional.
        return cash + long_notional - short_notional

    for _, kind, t in events:
        if kind == 0:
            if id(t) in opened:
                gross = notional_by_id.pop(id(t), t.qty * t.entry_price)
                if t.action == "BUY":
                    long_notional -= gross
                    total_cost = (t.entry_price + t.exit_price) * t.qty * txn_cost_pct
                    cash += t.qty * t.exit_price - total_cost
                else:
                    short_notional -= gross
                    total_cost = (t.entry_price + t.exit_price) * t.qty * txn_cost_pct
                    cash -= t.qty * t.exit_price + total_cost
            continue

        if t.entry_price <= 0:
            rejected += 1
            continue

        account_equity = equity()
        if account_equity <= 0:
            rejected += 1
            continue

        notional_max_shares = int((account_equity * max_position_pct) / t.entry_price)
        if position_sizing == "risk":
            stop_distance = None
            if t.stop_loss is not None:
                stop_distance = abs(t.entry_price - t.stop_loss)
            elif t.trailing_stop_pct is not None:
                stop_distance = t.entry_price * t.trailing_stop_pct
            if stop_distance and stop_distance > 0:
                desired_qty = int((account_equity * risk_per_trade_pct) / stop_distance)
            else:
                desired_qty = notional_max_shares
        else:
            # pattern/notional/atr modes: keep the signal-time qty from
            # _apply_sizing; still subject to the current account-equity cap.
            desired_qty = t.qty

        qty = min(desired_qty, notional_max_shares)
        if t.action == "BUY":
            qty = min(qty, int(cash / t.entry_price))

        if max_gross_exposure_pct and max_gross_exposure_pct > 0 and t.entry_price > 0:
            gross_cap = account_equity * max_gross_exposure_pct
            room = gross_cap - (long_notional + short_notional)
            qty = min(qty, int(room / t.entry_price)) if room > 0 else 0

        if qty < 1:
            rejected += 1
            continue
        if (
            settings.min_position_notional > 0
            and qty * t.entry_price < settings.min_position_notional
        ):
            rejected += 1
            continue

        t.qty = qty
        notion = qty * t.entry_price
        if t.action == "BUY":
            cash -= notion
            long_notional += notion
        else:
            # Short-sale proceeds increase cash, but the matching short
            # liability is recorded separately and therefore does not
            # increase equity. This avoids treating short proceeds as
            # deployable capital.
            cash += notion
            short_notional += notion
        notional_by_id[id(t)] = notion
        opened.add(id(t))
        accepted.append(t)

    return accepted, rejected


def _core_backtest_symbol(
    symbol: str,
    timeframe: str,
    candles: list[OHLCVCandle],
    patterns: list[BasePattern],
    config: dict,
    cooldown: dict | None = None,
) -> tuple[list[BacktestTrade], int]:
    if len(candles) < 1:
        return [], 0

    from data.edgar_client import set_skip_edgar

    set_skip_edgar(bool(config.get("skip_edgar")))

    # Bounded window, not the full backtest length — every pattern's
    # MIN_BARS tops out at 210, regime filter needs 200, Kronos gate LOOKBACK=400,
    # so DEFAULT_WINDOW is enough. Sizing this to len(candles) made every
    # per-bar indicator recompute run over the *entire* history so far,
    # turning a multi-year walk-forward into an O(n^2) crawl.
    store = OHLCVStore(
        window=DEFAULT_WINDOW,
        session_tz=config.get("session_tz") or "America/New_York",
    )
    trades: list[BacktestTrade] = []
    signals_count = 0
    pending_entry: TradeSignal | None = None
    open_position: BacktestTrade | None = None

    min_bars = _min_required_bars(timeframe)
    start = max(min_bars, 1)
    i = start
    cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {} if cooldown is None else cooldown  # type: ignore[assignment]

    # Seed the store with the initial window once, then grow it one candle
    # at a time as `i` advances — avoids re-slicing/rebuilding the whole
    # history into a fresh deque on every bar (was O(n^2) over the walk).
    store.replace_all(symbol, timeframe, candles[: i + 1])

    while i < len(candles):
        if i > start:
            store.append_candle(symbol, timeframe, candles[i])

        if open_position is not None:
            _update_neckline_state(open_position, candles[i], i)
            exit_price, exit_reason = _check_exit(
                candles[i], open_position, i,
                min_hold_bars=config["min_hold_bars"],
            )
            if exit_price is not None:
                _close_trade(
                    open_position, exit_price, exit_reason, candles[i],
                    config["txn_cost_pct"],
                )
                trades.append(open_position)
                key = (open_position.symbol, open_position.pattern)
                cooldown_tracker[key] = (i, open_position.pnl < 0)
                open_position = None
                i += 1
                continue
            _update_trailing_reference(open_position, candles[i])
            i += 1
            continue

        if pending_entry is not None:
            candidate = _open_trade(pending_entry, candles[i], i)
            pending_entry = None
            if not _execution_reward_risk_ok(
                candidate, config.get("min_reward_risk_ratio")
            ):
                # A gap between signal and fill can change the actual R:R.
                # Do not enter a trade that only passed the gate at signal time.
                i += 1
                continue
            open_position = candidate
            open_position.breakeven_trigger_pct = config["breakeven_trigger_pct"]
            open_position.breakeven_buffer_pct = config["breakeven_buffer_pct"]
            i += 1
            continue

        snapshot = _make_snapshot(symbol, timeframe, candles[i])
        for pattern in patterns:
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

            if not passes_min_confidence(signal, config["min_confidence"]):
                continue

            if not passes_min_share_price(
                signal, config.get("min_share_price"),
            ):
                continue

            if config.get("kronos_gate"):
                gate = kronos_gate_check(signal, store)
                if not gate.passed:
                    log.debug(
                        f"Backtest | {symbol} {timeframe} Kronos gate — skip "
                        f"({gate.reason})"
                    )
                    continue

            if config.get("volume_gate"):
                vgate = volume_confirm_gate(
                    signal, store,
                    rvol_min=config.get("volume_gate_rvol_min"),
                    obv_bars=config.get("volume_gate_obv_bars"),
                )
                if not vgate.passed:
                    log.debug(
                        f"Backtest | {symbol} {timeframe} Volume gate — skip "
                        f"({vgate.reason})"
                    )
                    continue
            else:
                # Still tag metrics for A/B post-analysis when gate is off.
                rvol, slope = compute_volume_metrics(
                    store, symbol, timeframe,
                    obv_bars=config.get("volume_gate_obv_bars"),
                )
                signal.rvol = rvol
                signal.obv_slope = slope

            if not passes_regime_filter(
                signal, store, enabled=config["regime_filter"],
            ):
                continue

            if not passes_cooldown(
                signal, i, cooldown_tracker,
                cooldown_bars=config["cooldown_bars"],
            ):
                continue

            if not apply_risk_gates(
                signal, store, symbol, timeframe,
                min_atr_stop_multiple=config["min_atr_stop_multiple"],
                synthetic_stop_multiple=config["synthetic_stop_multiple"],
                atr_stop_floor_multiple=config["atr_stop_floor_multiple"],
                hard_stop_percentage=config["hard_stop_percentage"],
                min_reward_risk_ratio=config["min_reward_risk_ratio"],
                trailing_activation_default=config["trailing_activation_default"],
            ):
                continue

            if config.get("long_only") and signal.action == "SELL":
                continue

            _apply_sizing(
                signal, store, symbol, timeframe,
                config["account_value"],
                config["risk_per_trade_pct"],
                config["position_sizing"],
                max_position_pct=config["max_position_pct"],
            )
            # Record the event bar before deferring the entry to i+1.
            signal.signal_bar_idx = i
            signal.signal_bar_timestamp = candles[i].timestamp
            if config.get("lot_round"):
                signal.price = signal.price or candles[i].close
                if not apply_lot_rounding(signal):
                    continue

            pending_entry = signal
            break

        i += 1

    if pending_entry is not None and len(candles) > 0:
        open_position = _open_trade(
            pending_entry, candles[-1], len(candles) - 1
        )
    if open_position is not None:
        _close_trade(
            open_position, candles[-1].close, "end_of_data", candles[-1],
            config["txn_cost_pct"],
        )
        trades.append(open_position)

    return trades, signals_count


# ── OHLCV caching + weekly derivation ────────────────────────────────────────


def _cache_path(symbol: str, timeframe: str, market: str | None = None) -> Path:
    return _CACHE_DIR / f"{ohlcv_cache_key(symbol, timeframe, market)}.json"


def _load_cached_ohlcv(
    symbol: str, timeframe: str, market: str | None = None,
) -> list[OHLCVCandle] | None:
    p = _cache_path(symbol, timeframe, market)
    if not p.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds()
        if age > _CACHE_TTL_SECONDS:
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [
            OHLCVCandle(
                open=c["o"], high=c["h"], low=c["l"], close=c["c"],
                volume=c.get("v", 0.0),
                timestamp=datetime.fromisoformat(c["t"]),
            )
            for c in raw
        ]
    except Exception:
        return None


def _save_cached_ohlcv(
    symbol: str, timeframe: str, candles: list[OHLCVCandle],
    market: str | None = None,
) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "o": c.open, "h": c.high, "l": c.low, "c": c.close,
            "v": c.volume, "t": c.timestamp.isoformat() if c.timestamp else "",
        }
        for c in candles
    ]
    _cache_path(symbol, timeframe, market).write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _derive_weekly_from_daily(
    daily: list[OHLCVCandle], session_tz: str = "America/New_York",
) -> list[OHLCVCandle]:
    if len(daily) < 5:
        return []
    df = pd.DataFrame([
        {
            "timestamp": c.timestamp or datetime.now(timezone.utc),
            "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "volume": c.volume,
        }
        for c in daily
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_convert(session_tz)
    df = df.set_index("timestamp").sort_index()
    weekly = df.resample("W-FRI", label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return [
        OHLCVCandle(
            open=row.open, high=row.high, low=row.low,
            close=row.close, volume=row.volume,
            timestamp=idx.to_pydatetime(),
        )
        for idx, row in weekly.iterrows()
    ]


def _worker_symbol_backtest(
    symbol: str,
    timeframe: str,
    screener: str,
    exchange: str,
    pattern_specs: list[tuple[str, str]],
    config: dict,
    candles: list[OHLCVCandle] | None = None,
) -> tuple[list[BacktestTrade], int]:
    if candles is None:
        tv = TVClient(screener, exchange)
        candles = tv._fetch_history_chart(symbol, timeframe)
    patterns = _load_patterns(pattern_specs)
    return _core_backtest_symbol(symbol, timeframe, candles, patterns, config)


class Backtester:
    def __init__(
        self,
        symbols: list[str],
        min_confidence: float = ENGINE.min_confidence,
        regime_filter: bool = ENGINE.regime_filter,
        cooldown_bars: int = ENGINE.cooldown_bars,
        txn_cost_pct: float = ENGINE.txn_cost_pct,
        position_sizing: str = ENGINE.position_sizing,
        account_value: float = ENGINE.account_value,
        risk_per_trade_pct: float = ENGINE.risk_per_trade_pct,
        max_position_pct: float = ENGINE.max_position_pct,
        max_gross_exposure_pct: float = ENGINE.max_gross_exposure_pct,
        trailing_activation_default: float | None = ENGINE.trailing_activation_default,
        max_open_positions: int | None = None,
        min_hold_bars: int = ENGINE.min_hold_bars,
        breakeven_trigger_pct: float | None = ENGINE.breakeven_trigger_pct,
        breakeven_buffer_pct: float = ENGINE.breakeven_buffer_pct,
        min_atr_stop_multiple: float | None = ENGINE.min_atr_stop_multiple,
        synthetic_stop_multiple: float = ENGINE.synthetic_stop_multiple,
        atr_stop_floor_multiple: float | None = ENGINE.atr_stop_floor_multiple,
        hard_stop_percentage: float | None = ENGINE.hard_stop_percentage,
        min_reward_risk_ratio: float | None = ENGINE.min_reward_risk_ratio,
        pattern_filter: str | None = None,
        disabled_patterns: list[str] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        max_workers: int = 0,
        kronos_gate: bool | None = None,
        volume_gate: bool | None = None,
        kronos_rank: bool | None = None,
        market: str | None = None,
        long_only: bool | None = None,
    ):
        self._symbols = symbols
        profile = get_market(market)
        self._market = profile.id
        from data.edgar_client import set_skip_edgar

        set_skip_edgar(profile.skip_edgar)
        self._long_only = profile.long_only if long_only is None else long_only
        self._tv = TVClient(profile.tv_screener, profile.tv_exchange)
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._pattern_filter = pattern_filter
        self._disabled_patterns = set(disabled_patterns or [])
        self._discover_patterns()

        self._min_confidence = min_confidence
        self._regime_filter = regime_filter
        self._kronos_gate = (
            profile.kronos_gate_default if kronos_gate is None else kronos_gate
        )
        self._volume_gate = (
            settings.volume_gate_enabled if volume_gate is None else volume_gate
        )
        self._kronos_rank = (
            profile.kronos_rank_default if kronos_rank is None else kronos_rank
        )
        self._cooldown_bars = cooldown_bars
        self._txn_cost_pct = txn_cost_pct
        self._position_sizing = position_sizing
        self._account_value = account_value
        self._risk_per_trade_pct = risk_per_trade_pct
        # Diversification ceiling — see _apply_sizing. Was hardcoded to 0.02
        # inside _apply_sizing, which silently capped every trade at 2% of
        # account notional and made risk_per_trade_pct a no-op for any
        # realistic stop distance. Now a real, independent knob.
        self._max_position_pct = max_position_pct
        self._max_gross_exposure_pct = max_gross_exposure_pct
        self._trailing_activation_default = trailing_activation_default
        self._max_open_positions = (
            settings.max_open_positions if max_open_positions is None else max_open_positions
        )
        self._min_hold_bars = min_hold_bars
        # ── Execution-layer, non-pattern risk controls ──────────────────────
        # These sit on top of whatever stop/target/trailing values a pattern
        # supplies; they never change a pattern's own signal logic.
        self._breakeven_trigger_pct = breakeven_trigger_pct
        self._breakeven_buffer_pct = breakeven_buffer_pct
        self._min_atr_stop_multiple = min_atr_stop_multiple
        self._synthetic_stop_multiple = synthetic_stop_multiple
        # Widens (never tightens) a pattern's own stop_loss up to
        # atr_stop_floor_multiple × ATR(14) when the pattern's structural
        # stop is tighter than that — keeps an unusually tight stop from
        # being ordinary daily noise rather than a real thesis failure.
        self._atr_stop_floor_multiple = atr_stop_floor_multiple
        # Hard loss cap from entry (e.g. 0.05 = -5% absolute stop). When set
        # the engine guarantees a stop_loss no worse than -hard_stop_percentage of
        # entry price, applied ONLY when the pattern itself supplies no
        # tighter stop. Acts as catastrophic-tail backstop without
        # interfering with the pattern's normal trailing/target logic.
        self._hard_stop_percentage = hard_stop_percentage
        # Minimum reward-to-risk ratio required before a signal is accepted.
        # reward = |take_profit - entry|, risk = |entry - stop_loss|. Signals
        # whose R:R falls below this are skipped — this is an engine-level
        # entry filter that does not alter any pattern's own signal logic,
        # stop/target/trailing values, or confidence scoring.
        self._min_reward_risk_ratio = min_reward_risk_ratio

        self._cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {}
        self._progress_callback = progress_callback
        self._max_workers = max_workers if max_workers > 0 else (os.cpu_count() or 4)

    def _discover_patterns(self) -> None:
        for module_name, cls in _iter_pattern_classes():
            instance = cls()
            # disabled_patterns only applies to the default multi-pattern run —
            # an explicit pattern_filter (testing one pattern in isolation)
            # always wins, even if that pattern is disabled by default.
            if instance.skipped:
                continue
            if self._pattern_filter is None and instance.name in self._disabled_patterns:
                continue
            # Apply pattern filter: match by case-insensitive substring
            if self._pattern_filter is not None:
                filter_lower = self._pattern_filter.lower()
                if filter_lower not in instance.name.lower():
                    continue
            self._patterns.append(instance)
            self._pattern_files[instance.name] = f"patterns/{module_name}.py"
            log.info(f"Backtester | Registered pattern: {instance}")

    async def run(self) -> BacktestResult:
        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        from tqdm import tqdm

        tasks = [(s, tf) for s in self._symbols for tf in sorted(all_timeframes)]
        result = BacktestResult(initial_capital=self._account_value)
        total_tasks = len(tasks)
        if total_tasks == 0:
            return result

        # ── Phase 1: Pre-fetch all daily OHLCV (cache → HTTP fallback) ─────
        need_weekly = "1W" in all_timeframes
        ohlcv_data: dict[tuple[str, str], list[OHLCVCandle]] = {}
        loop = asyncio.get_running_loop()

        async def _fetch_one(symbol: str):
            candles = _load_cached_ohlcv(symbol, "1d", self._market)
            if candles is None:
                candles = await asyncio.to_thread(
                    self._tv._fetch_history_chart, symbol, "1d"
                )
                if candles:
                    _save_cached_ohlcv(symbol, "1d", candles, self._market)
            if candles:
                ohlcv_data[(symbol, "1d")] = candles
                if need_weekly:
                    weekly = _derive_weekly_from_daily(
                        candles, get_market(self._market).session_tz
                    )
                    if weekly:
                        ohlcv_data[(symbol, "1W")] = weekly

        pbar_fetch = tqdm(
            total=len(self._symbols), desc="Fetching OHLCV",
            unit="sym", ncols=80,
        )

        async def _fetch_with_progress(symbol: str):
            await _fetch_one(symbol)
            pbar_fetch.update(1)

        await asyncio.gather(*[_fetch_with_progress(s) for s in self._symbols])
        pbar_fetch.close()

        # ── Phase 2: Build config, dispatch backtest workers ───────────────
        pattern_specs = [
            (type(p).__module__, type(p).__qualname__) for p in self._patterns
        ]
        config = {
            "min_confidence": self._min_confidence,
            "regime_filter": self._regime_filter,
            "cooldown_bars": self._cooldown_bars,
            "txn_cost_pct": self._txn_cost_pct,
            "position_sizing": self._position_sizing,
            "account_value": self._account_value,
            "risk_per_trade_pct": self._risk_per_trade_pct,
            "max_position_pct": self._max_position_pct,
            "max_gross_exposure_pct": self._max_gross_exposure_pct,
            "trailing_activation_default": self._trailing_activation_default,
            "min_hold_bars": self._min_hold_bars,
            "breakeven_trigger_pct": self._breakeven_trigger_pct,
            "breakeven_buffer_pct": self._breakeven_buffer_pct,
            "min_atr_stop_multiple": self._min_atr_stop_multiple,
            "synthetic_stop_multiple": self._synthetic_stop_multiple,
            "atr_stop_floor_multiple": self._atr_stop_floor_multiple,
            "hard_stop_percentage": self._hard_stop_percentage,
            "min_reward_risk_ratio": self._min_reward_risk_ratio,
            "kronos_gate": self._kronos_gate,
            "volume_gate": self._volume_gate,
            "volume_gate_rvol_min": settings.volume_gate_rvol_min,
            "volume_gate_obv_bars": settings.volume_gate_obv_bars,
            "kronos_rank": self._kronos_rank,
            "kronos_rank_top_k": settings.kronos_rank_top_k,
            "kronos_rank_bottom_k": settings.kronos_rank_bottom_k,
            "kronos_rank_long_only": (
                True if self._long_only else settings.kronos_rank_long_only
            ),
            "kronos_rank_min_move_pct": settings.kronos_rank_min_move_pct,
            "kronos_rank_rebalance_bars": settings.kronos_rank_rebalance_bars,
            "max_open_positions": self._max_open_positions,
            "long_only": self._long_only,
            "min_share_price": get_market(self._market).min_share_price,
            "skip_edgar": get_market(self._market).skip_edgar,
            "lot_round": get_market(self._market).lot_round,
            "session_tz": get_market(self._market).session_tz,
            "market": self._market,
        }

        max_workers = max(1, self._max_workers)
        pbar = tqdm(total=len(tasks), desc="Backtesting", unit="sym", ncols=80)

        pool_started = False
        try:
            ctx = mp.get_context("spawn")
            pool = ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx)
            with pool:
                pool_started = True
                futures = [
                    loop.run_in_executor(
                        pool, _worker_symbol_backtest,
                        s, tf,
                        get_market(self._market).tv_screener,
                        get_market(self._market).tv_exchange,
                        pattern_specs, config,
                        ohlcv_data.get((s, tf)),
                    )
                    for s, tf in tasks
                ]

                completed = 0
                for coro in asyncio.as_completed(futures):
                    # A single symbol/pattern raising here must not throw away
                    # every already-completed result and fall back to a fully
                    # serial re-run of the whole task list — log and skip it.
                    try:
                        trades, signals = await coro
                    except Exception:
                        log.warning(
                            "Backtester | worker task failed, skipping",
                            exc_info=True,
                        )
                        completed += 1
                        pbar.update(1)
                        continue
                    result.trades.extend(trades)
                    result.total_signals += signals
                    completed += 1
                    pbar.update(1)
                    if self._progress_callback is not None:
                        self._progress_callback(completed, total_tasks)
        except Exception:
            if pool_started:
                raise
            log.warning(
                "Backtester | ProcessPoolExecutor failed to start, "
                "falling back to thread pool"
            )
            sem = asyncio.Semaphore(max_workers)

            async def _backtest_one(symbol: str, timeframe: str):
                async with sem:
                    trades, signals = await asyncio.to_thread(
                        self._backtest_symbol, symbol, timeframe
                    )
                    pbar.update(1)
                    return trades, signals

            completed = 0
            for coro in asyncio.as_completed(
                [_backtest_one(s, tf) for s, tf in tasks]
            ):
                trades, signals = await coro
                result.trades.extend(trades)
                result.total_signals += signals
                completed += 1
                if self._progress_callback is not None:
                    self._progress_callback(completed, total_tasks)

        pbar.close()

        if self._kronos_rank:
            log.info("Backtester | running Kronos ranked forecast sleeve (cross-sectional)")
            from core.kronos_rank_sleeve import backtest_rank_sleeve

            ohlcv_1d = {
                s: candles
                for (s, tf), candles in ohlcv_data.items()
                if tf == "1d" and candles
            }
            sleeve_trades, sleeve_signals = await asyncio.to_thread(
                backtest_rank_sleeve, ohlcv_1d, config,
            )
            result.trades.extend(sleeve_trades)
            result.total_signals += sleeve_signals
            log.info(
                f"Backtester | KronosRank added {len(sleeve_trades)} trades "
                f"({sleeve_signals} signals)"
            )

        result.trades = _enforce_max_open_positions(
            result.trades,
            self._max_open_positions,
            max_open_per_pattern=settings.max_open_per_pattern,
        )
        result.trades, result.capital_rejected = _apply_capital_ledger(
            result.trades, self._account_value, self._risk_per_trade_pct,
            self._position_sizing, self._max_position_pct,
            max_gross_exposure_pct=self._max_gross_exposure_pct,
            txn_cost_pct=self._txn_cost_pct,
        )
        result.equity_curve = _build_portfolio_equity_curve(
            result.trades, ohlcv_data, self._account_value, self._txn_cost_pct,
        )
        return result

    def _backtest_symbol(
        self, symbol: str, timeframe: str
    ) -> tuple[list[BacktestTrade], int]:
        candles = self._fetch_history(symbol, timeframe)
        return _core_backtest_symbol(
            symbol, timeframe, candles, self._patterns,
            {
                "min_confidence": self._min_confidence,
                "regime_filter": self._regime_filter,
                "cooldown_bars": self._cooldown_bars,
                "txn_cost_pct": self._txn_cost_pct,
                "position_sizing": self._position_sizing,
                "account_value": self._account_value,
                "risk_per_trade_pct": self._risk_per_trade_pct,
                "max_position_pct": self._max_position_pct,
                "max_gross_exposure_pct": self._max_gross_exposure_pct,
                "trailing_activation_default": self._trailing_activation_default,
                "min_hold_bars": self._min_hold_bars,
                "breakeven_trigger_pct": self._breakeven_trigger_pct,
                "breakeven_buffer_pct": self._breakeven_buffer_pct,
                "min_atr_stop_multiple": self._min_atr_stop_multiple,
                "synthetic_stop_multiple": self._synthetic_stop_multiple,
                "atr_stop_floor_multiple": self._atr_stop_floor_multiple,
                "hard_stop_percentage": self._hard_stop_percentage,
                "min_reward_risk_ratio": self._min_reward_risk_ratio,
                "kronos_gate": self._kronos_gate,
                "volume_gate": self._volume_gate,
                "volume_gate_rvol_min": settings.volume_gate_rvol_min,
                "volume_gate_obv_bars": settings.volume_gate_obv_bars,
                "long_only": self._long_only,
                "min_share_price": get_market(self._market).min_share_price,
                "lot_round": get_market(self._market).lot_round,
                "session_tz": get_market(self._market).session_tz,
                "market": self._market,
            },
            self._cooldown_tracker,
        )

    # ── Sizing ──────────────────────────────────────────────────────────────────
    def _apply_sizing(
        self,
        signal: TradeSignal,
        store: OHLCVStore,
        symbol: str,
        timeframe: str,
        entry_price: float | None = None,
    ) -> None:
        _apply_sizing(
            signal, store, symbol, timeframe,
            self._account_value, self._risk_per_trade_pct,
            self._position_sizing, entry_price,
            max_position_pct=self._max_position_pct,
        )

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
            fills = []
            for level, reason in candidates:
                fill = _gap_aware_trigger_fill(
                    candle, level, is_short=is_short, favorable=False,
                )
                if fill is not None:
                    fills.append((fill, reason))
            if fills:
                # Mirror module-level _check_exit: pick the worst plausible
                # protective fill when several stops are crossed in one bar.
                if is_short:
                    fill, reason = max(fills, key=lambda f: f[0])
                else:
                    fill, reason = min(fills, key=lambda f: f[0])
                return fill, reason

        if position.take_profit is not None:
            fill = _gap_aware_trigger_fill(
                candle, position.take_profit, is_short=is_short, favorable=True,
            )
            if fill is not None:
                return fill, "take_profit"

        if _time_exit_ready(position, candle, bar_idx):
            position.time_exit_bars_elapsed = (
                bar_idx - position.neckline_break_bar_idx
            )
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
        # See module-level _open_trade: a rebased target that lands on the
        # wrong side of entry_price would trip on the very next tick at a
        # loss instead of a gain — drop it rather than act on it.
        if signal.action == "BUY":
            if stop_loss is not None and stop_loss >= entry_price:
                stop_loss = None
            if take_profit is not None and take_profit <= entry_price:
                take_profit = None
        else:
            if stop_loss is not None and stop_loss <= entry_price:
                stop_loss = None
            if take_profit is not None and take_profit >= entry_price:
                take_profit = None
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
            time_exit_only_unfavorable=signal.time_exit_only_unfavorable,
            trailing_stop_pct=signal.trailing_stop_pct,
            trailing_stop_mode=signal.trailing_stop_mode,
            trailing_activation_pct=signal.trailing_activation_pct,
            entry_bar_idx=bar_idx,
            confidence=signal.confidence,
            qty=signal.qty,
            notes=signal.notes,
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
        # Preserve the breakout event across the deferred next-bar fill.
        if (
            position.neckline is not None
            and position.neckline_break_bar_idx is None
            and signal.signal_bar_idx is not None
            and position.exit_bars_after_neckline_break is not None
            and position.neckline_break_direction is not None
        ):
            position.neckline_break_bar_idx = signal.signal_bar_idx
        elif position.neckline is not None and position.neckline_break_bar_idx is None:
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

    @staticmethod
    def _min_required_bars(timeframe: str) -> int:
        return _min_required_bars(timeframe)

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
