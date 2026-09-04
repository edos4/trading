"""
core/backtester.py — offline walk-forward pattern-backtest engine.

Replays daily OHLCV (from data/barcache/, one JSON per symbol) through the
registered chart patterns bar-by-bar. Each pattern's ``analyze()`` fires a
``TradeSignal`` only on the bar its entry trigger completes; the engine then
walks that trade forward through a fixed per-pattern exit ladder:

    hard stop  ->  target  ->  channel reclaim  ->  trailing stop  ->  time stop
    ->  end-of-data fallback

Every trade is a flat ``ENGINE.position_notional`` (default $10,000), independent,
no compounding, no portfolio caps — matching the locked ``.cjs`` pattern-backtest
scripts at C:\\Users\\dell\\tradingview-mcp. There is no ML gate, confidence gate,
regime filter, cooldown, risk-based sizing, or engine-level exit overlay: the
signal's own stop / target / trailing / neckline values are the whole story.

The same ``_open_trade`` / ``_check_exit`` / ``_close_trade`` functions drive
``core.paper_trader.PaperAccount`` so paper and backtest stay byte-identical.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import multiprocessing as mp
import os
import pkgutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import pandas as pd

import patterns as patterns_pkg
from core.engine_defaults import ENGINE, is_fractional_qty
from core.market import apply_lot_rounding, get_market
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, TradeSignal, skip_pattern_module
from utils.logger import log

# Legacy live-fetch cache (still used when no barcache_dir is given).
_CACHE_DIR = Path("data/cache")
_CACHE_TTL_SECONDS = 6 * 3600


# ── Trade record ─────────────────────────────────────────────────────────────
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
    pnl: float          # per-share $ after txn cost
    pnl_pct: float
    stop_loss: float | None = None
    stop_loss_on_close: bool = False
    # Dual stop: effective stop is the nearer-to-entry of the structural
    # stop_loss and entry*(1 ± stop_loss_pct_cap). (`.cjs` upward-channel C24.)
    stop_loss_pct_cap: float | None = None
    take_profit: float | None = None
    neckline: float | None = None
    neckline_break_direction: Literal["below", "above"] | None = None
    exit_bars_after_neckline_break: int | None = None
    exit_bars_after_entry: int | None = None
    trailing_stop_pct: float | None = None
    trailing_stop_mode: Literal[
        "highest_close", "lowest_close", "highest_high", "lowest_low",
        "highest_low", "lowest_high",
    ] | None = None
    trailing_stop_on_close: bool = False
    trailing_activation_pct: float | None = None
    # Channel-reclaim exit (`.cjs` upward-channel C21): close back above the
    # rising lower rail with a higher-high + higher-low vs the prior bar.
    reclaim_exit: bool = False
    reclaim_lower_rail: tuple[float, float] | None = None  # (rail@entry, slope/bar)
    entry_bar_idx: int = -1
    neckline_break_bar_idx: int | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    lowest_close_since_entry: float | None = None
    highest_close_since_entry: float | None = None
    lowest_low_since_entry: float | None = None
    highest_high_since_entry: float | None = None
    highest_low_since_entry: float | None = None
    lowest_high_since_entry: float | None = None
    exit_reason: str = ""
    confidence: float = 0.0
    qty: float = 0.0
    notes: str = ""
    rvol: float | None = None
    obv_slope: float | None = None

    # Paper trading overwrites entry_date/exit_date with the real wall-clock
    # fill time; these keep the bar's own timestamp for simulated hold time.
    sim_entry_date: datetime | None = None
    sim_exit_date: datetime | None = None
    exit_bar_idx: int | None = None
    time_exit_bars_elapsed: int | None = None

    # One mark per market session while open (paper trading).
    position_marks: list[dict] = field(default_factory=list)

    _trailing_activated: bool = False
    _best_pnl_pct: float | None = None

    def __str__(self) -> str:
        return (
            f"{self.entry_date.strftime('%Y-%m-%d')} "
            f"{self.action:4s} {self.symbol:6s} {self.timeframe} "
            f"entry={self.entry_price:.2f} exit={self.exit_price:.2f} "
            f"pnl={self.pnl_pct:+.2f}% ({self.exit_reason})"
        )

    @property
    def pnl_usd(self) -> float:
        return self.pnl * self.qty

    @property
    def days_held(self) -> float:
        return (self.exit_date - self.entry_date).total_seconds() / 86400


# ── Trade math (shared by reporting + display) ───────────────────────────────
def trade_r_multiple(trade: BacktestTrade, price: float) -> float | None:
    """Gain/loss in multiples of the initial stop distance. None if no stop."""
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


def initial_risk_pct(trade: BacktestTrade) -> float | None:
    if trade.stop_loss is None or trade.entry_price <= 0:
        return None
    risk_pct = abs(trade.entry_price - trade.stop_loss) / trade.entry_price
    return risk_pct if risk_pct > 0 else None


# ── Result / metrics (the `.cjs` summarize() set + profit factor) ────────────
def _summarize(trades: list[BacktestTrade], notional: float) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    total_usd = sum(t.pnl_usd for t in trades)
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses if t.pnl_usd < 0)
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t.exit_reason or "unknown"] += 1
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss_usd = (sum(t.pnl_usd for t in losses) / len(losses)) if losses else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_pnl_pct": round(sum(t.pnl_pct for t in trades) / n, 2) if n else 0.0,
        "total_usd": round(total_usd, 2),
        "worst_usd": round(min((t.pnl_usd for t in trades), default=0.0), 2),
        "best_usd": round(max((t.pnl_usd for t in trades), default=0.0), 2),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss_usd, 2),
        "profit_factor": (
            round(gross_win / gross_loss, 3) if gross_loss > 1e-9
            else (None if gross_win == 0 else float("inf"))
        ),
        "payoff_ratio": (
            round(avg_win / abs(avg_loss_usd), 3) if avg_loss_usd < 0 else None
        ),
        "roi_on_deployed_pct": (
            round(total_usd / (n * notional) * 100, 2) if n and notional else 0.0
        ),
        "by_exit_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "avg_hold_days": (
            round(sum(t.days_held for t in trades) / n, 1) if n else 0.0
        ),
    }


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    total_signals: int = 0
    # Trades a pattern's own filter refused (populated by pattern_006 only:
    # earnings blackout + C22 freshness + C23 don't-chase).
    blocked: list[dict] = field(default_factory=list)
    filtered: list[dict] = field(default_factory=list)
    position_notional: float = 10_000.0
    version: str = ""

    # ── scalar metrics (kept for existing callers / dashboards) ──────────
    @property
    def _s(self) -> dict:
        return _summarize(self.trades, self.position_notional)

    @property
    def win_count(self) -> int:
        return self._s["wins"]

    @property
    def loss_count(self) -> int:
        return self._s["losses"]

    @property
    def win_rate(self) -> float:
        return (self._s["win_rate_pct"] / 100.0) if self.trades else 0.0

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def avg_pnl_pct(self) -> float:
        return self._s["avg_pnl_pct"]

    @property
    def total_usd(self) -> float:
        return self._s["total_usd"]

    @property
    def worst_usd(self) -> float:
        return self._s["worst_usd"]

    @property
    def profit_factor(self) -> float:
        pf = self._s["profit_factor"]
        return float("inf") if pf is None else pf

    @property
    def exit_reason_breakdown(self) -> dict[str, int]:
        return self._s["by_exit_reason"]

    @property
    def avg_hold_bars(self) -> float:
        vals = [
            t.exit_bar_idx - t.entry_bar_idx
            for t in self.trades
            if t.exit_bar_idx is not None and t.entry_bar_idx >= 0
        ]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    def by_pattern(self) -> dict[str, list[BacktestTrade]]:
        groups: dict[str, list[BacktestTrade]] = defaultdict(list)
        for t in self.trades:
            groups[t.pattern].append(t)
        return dict(groups)

    def pattern_breakdown(self) -> dict[str, dict]:
        groups = self.by_pattern()
        ranked = sorted(
            groups.items(),
            key=lambda kv: _summarize(kv[1], self.position_notional)["total_usd"],
            reverse=True,
        )
        return {p: _summarize(ts, self.position_notional) for p, ts in ranked}

    def bucket_summary(
        self, buckets: dict[str, frozenset[str]] | None
    ) -> dict[str, dict]:
        out = {"combined": _summarize(self.trades, self.position_notional)}
        for name, members in (buckets or {}).items():
            ts = [t for t in self.trades if t.symbol.upper() in members]
            out[name] = _summarize(ts, self.position_notional)
        return out

    # ── output ──────────────────────────────────────────────────────────
    def summary(self, buckets: dict[str, frozenset[str]] | None = None) -> str:
        lines = [
            "=" * 66,
            f"  PATTERN BACKTEST{('  -  ' + self.version) if self.version else ''}",
            f"  ${self.position_notional:,.0f} per trade  -  {self.total_signals} signals",
            "=" * 66,
        ]
        for name, s in self.bucket_summary(buckets).items():
            if not s["trades"] and name != "combined":
                continue
            pf = "inf" if s["profit_factor"] in (None, float("inf")) else f"{s['profit_factor']:.2f}"
            lines += [
                f"  -- {name} " + "-" * max(0, 52 - len(name)),
                f"  Trades:    {s['trades']} ({s['wins']}W/{s['losses']}L)",
                f"  Win rate:  {s['win_rate_pct']:.1f}%",
                f"  Avg P&L:   {s['avg_pnl_pct']:+.2f}%",
                f"  Total:     ${s['total_usd']:,.0f}",
                f"  Worst:     ${s['worst_usd']:,.0f}    Best: ${s['best_usd']:,.0f}",
                f"  Profit factor: {pf}   Avg hold: {s['avg_hold_days']:.1f}d",
                f"  Exits:     " + "  ".join(
                    f"{k}={v}" for k, v in s["by_exit_reason"].items()
                ),
            ]
        if self.blocked:
            lines.append(f"  Blocked (earnings): {len(self.blocked)}")
        if self.filtered:
            lines.append(f"  Filtered (C22/C23): {len(self.filtered)}")
        breakdown = self.pattern_breakdown()
        if len(breakdown) > 1:
            lines += ["  -- by pattern " + "-" * 47]
            for pat, s in breakdown.items():
                pf = "inf" if s["profit_factor"] in (None, float("inf")) else f"{s['profit_factor']:.2f}"
                lines.append(
                    f"  {pat:34s} n={s['trades']:<3d} win={s['win_rate_pct']:.0f}% "
                    f"avg={s['avg_pnl_pct']:+.2f}% total=${s['total_usd']:,.0f} pf={pf}"
                )
        lines.append("=" * 66)
        return "\n".join(lines)

    def to_dict(self, buckets: dict[str, frozenset[str]] | None = None) -> dict:
        return {
            "meta": {
                "date": datetime.now(timezone.utc).isoformat(),
                "version": self.version,
                "notional": self.position_notional,
                "total_signals": self.total_signals,
                "trades": len(self.trades),
            },
            "summary": self.bucket_summary(buckets),
            "by_pattern": self.pattern_breakdown(),
            "trades": [
                {
                    "sym": t.symbol,
                    "pattern": t.pattern,
                    "timeframe": t.timeframe,
                    "action": t.action,
                    "entryDate": t.entry_date.isoformat(),
                    "entryPrice": round(t.entry_price, 4),
                    "shares": round(t.qty, 4),
                    "exitDate": t.exit_date.isoformat(),
                    "exitPrice": round(t.exit_price, 4),
                    "exitReason": t.exit_reason,
                    "daysHeld": round(t.days_held, 1),
                    "barsHeld": (
                        (t.exit_bar_idx - t.entry_bar_idx)
                        if t.exit_bar_idx is not None else None
                    ),
                    "stop": round(t.stop_loss, 4) if t.stop_loss else None,
                    "target": round(t.take_profit, 4) if t.take_profit else None,
                    "pnlPct": round(t.pnl_pct, 4),
                    "pnlUSD": round(t.pnl_usd, 2),
                    "notes": t.notes,
                }
                for t in sorted(self.trades, key=lambda x: x.entry_date)
            ],
            "blocked": self.blocked,
            "filtered": self.filtered,
        }

    def save(self, path: str, buckets: dict[str, frozenset[str]] | None = None) -> None:
        p = Path(path)
        lines = [self.summary(buckets), "", "  TRADES", "-" * 66]
        for t in sorted(self.trades, key=lambda t: t.entry_date):
            lines.append(
                f"  {t.entry_date.strftime('%Y-%m-%d')} {t.action:5s} {t.symbol:8s} "
                f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                f"pnl={t.pnl_pct:+.2f}% (${t.pnl_usd:+,.0f}) "
                f"reason={t.exit_reason} {t.pattern}"
            )
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info(f"Backtest | results saved to {p}")


# ── Snapshot / pattern-input plumbing ───────────────────────────────────────
def _min_required_bars(timeframe: str) -> int:
    return 65 if timeframe == "1W" else 120


def _make_snapshot(symbol: str, timeframe: str, candle: OHLCVCandle) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=candle.timestamp or datetime.now(timezone.utc),
        candle=candle,
        indicators={
            "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.volume,
        },
        summary={"RECOMMENDATION": "NEUTRAL"},
        oscillators={},
        moving_avgs={},
    )


# ── Exit-ladder helpers ─────────────────────────────────────────────────────
def _update_neckline_state(
    position: BacktestTrade, candle: OHLCVCandle, bar_idx: int
) -> None:
    if position.neckline is None or position.neckline_break_bar_idx is not None:
        return
    if position.neckline_break_direction == "below" and candle.close < position.neckline:
        position.neckline_break_bar_idx = bar_idx
    elif position.neckline_break_direction == "above" and candle.close > position.neckline:
        position.neckline_break_bar_idx = bar_idx


def _update_prev_hl(position: BacktestTrade, candle: OHLCVCandle) -> None:
    position.prev_high = candle.high
    position.prev_low = candle.low


def _update_trailing_reference(position: BacktestTrade, candle: OHLCVCandle) -> None:
    mode = position.trailing_stop_mode
    if mode == "highest_close":
        b = position.highest_close_since_entry
        position.highest_close_since_entry = candle.close if b is None else max(b, candle.close)
    elif mode == "lowest_close":
        b = position.lowest_close_since_entry
        position.lowest_close_since_entry = candle.close if b is None else min(b, candle.close)
    elif mode == "highest_high":
        b = position.highest_high_since_entry
        position.highest_high_since_entry = candle.high if b is None else max(b, candle.high)
    elif mode == "lowest_low":
        b = position.lowest_low_since_entry
        position.lowest_low_since_entry = candle.low if b is None else min(b, candle.low)
    elif mode == "highest_low":
        b = position.highest_low_since_entry
        position.highest_low_since_entry = candle.close if b is None else max(b, candle.close)
    elif mode == "lowest_high":
        b = position.lowest_high_since_entry
        position.lowest_high_since_entry = candle.close if b is None else min(b, candle.close)
    entry = position.entry_price
    if entry <= 0:
        return
    pnl = (
        (entry - candle.close) / entry if position.action == "SELL"
        else (candle.close - entry) / entry
    )
    if position._best_pnl_pct is None or pnl > position._best_pnl_pct:
        position._best_pnl_pct = pnl


def _trailing_reference(position: BacktestTrade) -> float | None:
    return {
        "highest_close": position.highest_close_since_entry,
        "lowest_close": position.lowest_close_since_entry,
        "highest_high": position.highest_high_since_entry,
        "lowest_low": position.lowest_low_since_entry,
        "highest_low": position.highest_low_since_entry,
        "lowest_high": position.lowest_high_since_entry,
    }.get(position.trailing_stop_mode)


def _trailing_stop_price(position: BacktestTrade, is_short: bool) -> float | None:
    if position.trailing_activation_pct is not None and not position._trailing_activated:
        if (
            position.trailing_activation_pct <= 0
            or (
                position._best_pnl_pct is not None
                and position._best_pnl_pct >= position.trailing_activation_pct
            )
        ):
            position._trailing_activated = True
        else:
            return None
    pct = position.trailing_stop_pct
    if pct is None or position.trailing_stop_mode is None:
        return None
    ref = _trailing_reference(position)
    if ref is None:
        return None
    return ref * (1 + pct) if is_short else ref * (1 - pct)


def _effective_stop(position: BacktestTrade) -> float | None:
    """Nearer-to-entry of the structural stop and the fixed % cap (`.cjs` C24)."""
    base = position.stop_loss
    cap = None
    if position.stop_loss_pct_cap is not None and position.entry_price > 0:
        c = position.stop_loss_pct_cap
        cap = (
            position.entry_price * (1 + c) if position.action == "SELL"
            else position.entry_price * (1 - c)
        )
    if base is None:
        return cap
    if cap is None:
        return base
    # short: both above entry -> tighter is the lower;  long: tighter is the higher
    return min(base, cap) if position.action == "SELL" else max(base, cap)


def _gap_aware_trigger_fill(
    candle: OHLCVCandle, trigger: float, *, is_short: bool, favorable: bool,
    prior_ref: float | None = None,
) -> float | None:
    """Realistic daily-bar fill for a triggered stop/target (open-through = gap)."""
    if trigger <= 0 or candle.open <= 0:
        return None
    if is_short:
        crossed = candle.low <= trigger if favorable else candle.high >= trigger
        gapped = candle.open <= trigger if favorable else candle.open >= trigger
        if not favorable and gapped and prior_ref is not None and trigger < prior_ref:
            gapped = False
    else:
        crossed = candle.high >= trigger if favorable else candle.low <= trigger
        gapped = candle.open >= trigger if favorable else candle.open <= trigger
        if not favorable and gapped and prior_ref is not None and trigger > prior_ref:
            gapped = False
    if not crossed and not gapped:
        return None
    return candle.open if gapped else trigger


def _reclaim_rail_at(position: BacktestTrade, bar_idx: int) -> float | None:
    if position.reclaim_lower_rail is None:
        return None
    rail0, slope = position.reclaim_lower_rail
    return rail0 + slope * (bar_idx - position.entry_bar_idx)


def _check_exit(
    candle: OHLCVCandle, position: BacktestTrade, bar_idx: int
) -> tuple[float | None, str]:
    """Fixed per-pattern exit ladder — see module docstring. No engine overlays."""
    is_short = position.action == "SELL"
    prior_ref = _trailing_reference(position)
    if prior_ref is None:
        prior_ref = position.entry_price
    _update_trailing_reference(position, candle)

    # 1. hard stop (dual: structural vs fixed % cap)
    stop = _effective_stop(position)
    if stop is not None:
        if position.stop_loss_on_close:
            hit = candle.close >= stop if is_short else candle.close <= stop
            if hit:
                position.exit_bar_idx = bar_idx
                return stop, "stop_loss"
        else:
            fill = _gap_aware_trigger_fill(
                candle, stop, is_short=is_short, favorable=False, prior_ref=prior_ref,
            )
            if fill is not None:
                position.exit_bar_idx = bar_idx
                return fill, "stop_loss"

    # 2. target (close-based, fill at the target price)
    if position.take_profit is not None:
        tp = position.take_profit
        hit = candle.close <= tp if is_short else candle.close >= tp
        if hit:
            position.exit_bar_idx = bar_idx
            return tp, "take_profit"

    # 3. channel reclaim (close back above the rising lower rail + HH/HL)
    if position.reclaim_exit and position.prev_high is not None:
        rail = _reclaim_rail_at(position, bar_idx)
        if (
            rail is not None
            and candle.close > rail
            and candle.high > position.prev_high
            and candle.low > (position.prev_low if position.prev_low is not None else candle.low)
        ):
            position.exit_bar_idx = bar_idx
            return candle.close, "reclaim"

    # 4. trailing stop
    trail = _trailing_stop_price(position, is_short)
    if trail is not None:
        if position.trailing_stop_on_close:
            hit = candle.close >= trail if is_short else candle.close <= trail
            if hit:
                position.exit_bar_idx = bar_idx
                return trail, "trailing_stop"
        else:
            fill = _gap_aware_trigger_fill(
                candle, trail, is_short=is_short, favorable=False, prior_ref=prior_ref,
            )
            if fill is not None:
                position.exit_bar_idx = bar_idx
                return fill, "trailing_stop"

    # 5. time stop
    bars_held = bar_idx - position.entry_bar_idx
    if (
        position.exit_bars_after_entry is not None
        and bars_held >= position.exit_bars_after_entry
    ):
        position.exit_bar_idx = bar_idx
        position.time_exit_bars_elapsed = bars_held
        return candle.close, "time_exit"
    if (
        position.neckline_break_bar_idx is not None
        and position.exit_bars_after_neckline_break is not None
    ):
        elapsed = bar_idx - position.neckline_break_bar_idx
        if elapsed >= position.exit_bars_after_neckline_break:
            position.exit_bar_idx = bar_idx
            position.time_exit_bars_elapsed = elapsed
            return candle.close, "time_exit"

    return None, ""


# ── Sizing ──────────────────────────────────────────────────────────────────
def _apply_notional_sizing(
    signal: TradeSignal,
    notional: float = 10_000.0,
    *,
    fractional: bool = False,
) -> None:
    """Flat notional per trade. floor(notional/price) shares, or fractional."""
    px = signal.price
    if px is None or px <= 0:
        signal.qty = 0.0
        return
    signal.qty = (notional / px) if fractional else float(int(notional // px))


# ── Trade lifecycle ─────────────────────────────────────────────────────────
def _open_trade(
    signal: TradeSignal, candle: OHLCVCandle, bar_idx: int
) -> BacktestTrade:
    entry_price = candle.close
    stop_loss = signal.stop_loss
    if stop_loss is not None and signal.price and signal.price > 0 and entry_price > 0:
        if signal.action == "BUY":
            stop_loss = round(entry_price * (1 - (signal.price - stop_loss) / signal.price), 4)
        else:
            stop_loss = round(entry_price * (1 + (stop_loss - signal.price) / signal.price), 4)
    take_profit = signal.take_profit
    if take_profit is not None and signal.price and signal.price > 0 and entry_price > 0:
        if signal.action == "BUY":
            take_profit = round(entry_price * (1 + (take_profit - signal.price) / signal.price), 4)
        else:
            take_profit = round(entry_price * (1 - (signal.price - take_profit) / signal.price), 4)
    # Drop a target/stop that rebased onto the wrong side of entry.
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

    ts = candle.timestamp or datetime.now(timezone.utc)
    position = BacktestTrade(
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        pattern=signal.pattern,
        action=signal.action,
        entry_date=ts,
        exit_date=ts,
        entry_price=entry_price,
        exit_price=entry_price,
        pnl=0.0,
        pnl_pct=0.0,
        stop_loss=stop_loss,
        stop_loss_on_close=signal.stop_loss_on_close,
        stop_loss_pct_cap=signal.stop_loss_pct_cap,
        take_profit=take_profit,
        neckline=signal.neckline,
        neckline_break_direction=signal.neckline_break_direction,
        exit_bars_after_neckline_break=signal.exit_bars_after_neckline_break,
        exit_bars_after_entry=signal.exit_bars_after_entry,
        trailing_stop_pct=signal.trailing_stop_pct,
        trailing_stop_mode=signal.trailing_stop_mode,
        trailing_stop_on_close=signal.trailing_stop_on_close,
        trailing_activation_pct=signal.trailing_activation_pct,
        reclaim_exit=signal.reclaim_exit,
        reclaim_lower_rail=signal.reclaim_lower_rail,
        entry_bar_idx=bar_idx,
        confidence=signal.confidence,
        qty=signal.qty,
        notes=signal.notes,
        rvol=signal.rvol,
        obv_slope=signal.obv_slope,
        prev_high=candle.high,
        prev_low=candle.low,
        lowest_close_since_entry=candle.close if signal.trailing_stop_mode == "lowest_close" else None,
        highest_close_since_entry=candle.close if signal.trailing_stop_mode == "highest_close" else None,
        lowest_low_since_entry=candle.low if signal.trailing_stop_mode == "lowest_low" else None,
        highest_high_since_entry=candle.high if signal.trailing_stop_mode == "highest_high" else None,
        highest_low_since_entry=candle.close if signal.trailing_stop_mode == "highest_low" else None,
        lowest_high_since_entry=candle.close if signal.trailing_stop_mode == "lowest_high" else None,
    )
    # Start the neckline time-stop clock only once the close is actually
    # through the neckline. If entry is a day-7 fill with no break yet,
    # _update_neckline_state starts the clock when the break really prints.
    if position.neckline is not None and position.neckline_break_bar_idx is None:
        if position.neckline_break_direction == "below" and candle.close < position.neckline:
            position.neckline_break_bar_idx = bar_idx
        elif position.neckline_break_direction == "above" and candle.close > position.neckline:
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
    pnl -= position.entry_price * txn_cost_pct + exit_price * txn_cost_pct
    position.exit_date = candle.timestamp or datetime.now(timezone.utc)
    position.exit_price = exit_price
    position.pnl = pnl
    position.pnl_pct = (pnl / position.entry_price) * 100 if position.entry_price else 0.0
    position.exit_reason = reason
    log.info(
        f"Backtest | EXIT {position.symbol} {position.timeframe} "
        f"reason={reason} pnl={position.pnl_pct:+.2f}%"
    )


# ── Pattern discovery ───────────────────────────────────────────────────────
def _load_patterns(pattern_specs: list[tuple[str, str]]) -> list[BasePattern]:
    out: list[BasePattern] = []
    for module_name, class_name in pattern_specs:
        module = importlib.import_module(module_name)
        out.append(getattr(module, class_name)())
    return out


def _iter_pattern_classes() -> list[tuple[str, type[BasePattern]]]:
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
    return sorted({
        inst.name for _, cls in _iter_pattern_classes()
        if not (inst := cls()).skipped
    })


# ── Per-symbol walk ─────────────────────────────────────────────────────────
def _core_backtest_symbol(
    symbol: str,
    timeframe: str,
    candles: list[OHLCVCandle],
    patterns: list[BasePattern],
    config: dict,
) -> tuple[list[BacktestTrade], int, list[dict], list[dict]]:
    if not candles:
        return [], 0, [], []

    store = OHLCVStore(
        window=DEFAULT_WINDOW,
        session_tz=config.get("session_tz") or "America/New_York",
    )
    trades: list[BacktestTrade] = []
    blocked: list[dict] = []
    filtered: list[dict] = []
    signals_count = 0
    open_position: BacktestTrade | None = None
    notional = config.get("position_notional", ENGINE.position_notional)
    txn_cost = config.get("txn_cost_pct", 0.0)
    lot_round = config.get("lot_round", False)

    min_bars = _min_required_bars(timeframe)
    start = max(min_bars, 1)
    i = start
    store.replace_all(symbol, timeframe, candles[: i + 1])
    # `.cjs` scripts never anchor a pattern whose full exit horizon can't be
    # simulated (loop bounds subtract confirm+maxHold+lb). Walk-forward
    # equivalent: don't open a fresh position in the last few bars, where an
    # instant `data_end` at ~0% would just be noise.
    open_cutoff = len(candles) - config.get("end_margin", 5)

    while i < len(candles):
        if i > start:
            store.append_candle(symbol, timeframe, candles[i])

        if open_position is not None:
            _update_neckline_state(open_position, candles[i], i)
            exit_price, exit_reason = _check_exit(candles[i], open_position, i)
            if exit_price is not None:
                _close_trade(open_position, exit_price, exit_reason, candles[i], txn_cost)
                trades.append(open_position)
                open_position = None
            else:
                _update_prev_hl(open_position, candles[i])
            i += 1
            continue

        if i >= open_cutoff:
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

            # pattern-level diagnostics (pattern_006 tags its own rejects here)
            if getattr(signal, "blocked_reason", None):
                blocked.append({
                    "sym": symbol,
                    "entryDate": (candles[i].timestamp or datetime.now(timezone.utc)).date().isoformat(),
                    "blockReason": signal.blocked_reason,
                })
                break
            if getattr(signal, "filtered_reason", None):
                filtered.append({
                    "sym": symbol,
                    "entryDate": (candles[i].timestamp or datetime.now(timezone.utc)).date().isoformat(),
                    "reason": signal.filtered_reason,
                })
                break

            _apply_notional_sizing(
                signal, notional, fractional=is_fractional_qty(signal.pattern),
            )
            if signal.qty < 1 and not is_fractional_qty(signal.pattern):
                break
            if signal.qty <= 0:
                break
            signal.signal_bar_idx = i
            signal.signal_bar_timestamp = candles[i].timestamp
            if lot_round:
                signal.price = signal.price or candles[i].close
                if not apply_lot_rounding(signal):
                    break

            open_position = _open_trade(signal, candles[i], i)
            break

        i += 1

    if open_position is not None:
        _close_trade(open_position, candles[-1].close, "data_end", candles[-1], txn_cost)
        trades.append(open_position)

    return trades, signals_count, blocked, filtered


# ── OHLCV loading (barcache first, legacy live cache fallback) ───────────────
def _load_barcache_candles(
    barcache_dir: str | Path, market: str, symbol: str
) -> list[OHLCVCandle] | None:
    from data.barcache import load as _load
    return _load(market, symbol, root=barcache_dir)


def _cache_path(symbol: str, timeframe: str, market: str | None = None) -> Path:
    from core.market import ohlcv_cache_key
    return _CACHE_DIR / f"{ohlcv_cache_key(symbol, timeframe, market)}.json"


def _load_cached_ohlcv(
    symbol: str, timeframe: str, market: str | None = None
) -> list[OHLCVCandle] | None:
    p = _cache_path(symbol, timeframe, market)
    if not p.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if (datetime.now(timezone.utc) - mtime).total_seconds() > _CACHE_TTL_SECONDS:
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [
            OHLCVCandle(
                open=c["o"], high=c["h"], low=c["l"], close=c["c"],
                volume=c.get("v", 0.0), timestamp=datetime.fromisoformat(c["t"]),
            )
            for c in raw
        ]
    except Exception:
        return None


def _save_cached_ohlcv(
    symbol: str, timeframe: str, candles: list[OHLCVCandle], market: str | None = None
) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume,
         "t": c.timestamp.isoformat() if c.timestamp else ""}
        for c in candles
    ]
    _cache_path(symbol, timeframe, market).write_text(json.dumps(payload), encoding="utf-8")


def _derive_weekly_from_daily(
    daily: list[OHLCVCandle], session_tz: str = "America/New_York"
) -> list[OHLCVCandle]:
    if len(daily) < 5:
        return []
    df = pd.DataFrame([
        {"timestamp": c.timestamp or datetime.now(timezone.utc), "open": c.open,
         "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in daily
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(session_tz)
    df = df.set_index("timestamp").sort_index()
    weekly = df.resample("W-FRI", label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    return [
        OHLCVCandle(
            open=row.open, high=row.high, low=row.low, close=row.close,
            volume=row.volume, timestamp=idx.to_pydatetime(),
        )
        for idx, row in weekly.iterrows()
    ]


def _worker_symbol_backtest(
    symbol: str,
    timeframe: str,
    pattern_specs: list[tuple[str, str]],
    config: dict,
    candles: list[OHLCVCandle] | None = None,
) -> tuple[list[BacktestTrade], int, list[dict], list[dict]]:
    if candles is None:
        return [], 0, [], []
    patterns = _load_patterns(pattern_specs)
    return _core_backtest_symbol(symbol, timeframe, candles, patterns, config)


# ── Orchestrator ────────────────────────────────────────────────────────────
class Backtester:
    def __init__(
        self,
        symbols: list[str],
        *,
        barcache_dir: str | Path | None = None,
        market: str | None = None,
        txn_cost_pct: float | None = None,
        position_notional: float = ENGINE.position_notional,
        min_bars: int = ENGINE.min_bars,
        pattern_filter: str | None = None,
        disabled_patterns: list[str] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        max_workers: int = 0,
        version: str = "",
        end_margin: int = 5,
    ):
        self._symbols = symbols
        profile = get_market(market)
        self._market = profile.id
        self._barcache_dir = str(barcache_dir) if barcache_dir else None
        self._txn_cost_pct = (
            profile.txn_cost_pct if txn_cost_pct is None else txn_cost_pct
        )
        self._position_notional = position_notional
        self._min_bars = min_bars
        self._version = version
        self._end_margin = end_margin
        self._pattern_filter = pattern_filter
        self._disabled_patterns = set(disabled_patterns or [])
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._discover_patterns()
        self._progress_callback = progress_callback
        self._max_workers = max_workers if max_workers > 0 else (os.cpu_count() or 4)

    def _discover_patterns(self) -> None:
        for module_name, cls in _iter_pattern_classes():
            instance = cls()
            if instance.skipped:
                continue
            if self._pattern_filter is None and instance.name in self._disabled_patterns:
                continue
            if (
                self._pattern_filter is not None
                and self._pattern_filter.lower() not in instance.name.lower()
            ):
                continue
            self._patterns.append(instance)
            self._pattern_files[instance.name] = f"patterns/{module_name}.py"
            log.info(f"Backtester | registered {instance}")

    def _config(self) -> dict:
        profile = get_market(self._market)
        return {
            "position_notional": self._position_notional,
            "txn_cost_pct": self._txn_cost_pct,
            "min_bars": self._min_bars,
            "end_margin": self._end_margin,
            "lot_round": profile.lot_round,
            "session_tz": profile.session_tz,
            "market": self._market,
        }

    def _load_candles(self, symbol: str) -> list[OHLCVCandle] | None:
        if self._barcache_dir:
            return _load_barcache_candles(self._barcache_dir, self._market, symbol)
        candles = _load_cached_ohlcv(symbol, "1d", self._market)
        if candles is None:
            from data.history import fetch_ohlcv_candles
            try:
                candles = fetch_ohlcv_candles(symbol, "1d", market=self._market)
            except Exception:
                log.warning(f"Backtester | fetch failed for {symbol}", exc_info=True)
                return None
            if candles:
                _save_cached_ohlcv(symbol, "1d", candles, self._market)
        return candles

    async def run(self) -> BacktestResult:
        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)
        result = BacktestResult(
            position_notional=self._position_notional, version=self._version,
        )
        tasks = [(s, tf) for s in self._symbols for tf in sorted(all_timeframes)]
        if not tasks:
            return result

        need_weekly = "1W" in all_timeframes
        session_tz = get_market(self._market).session_tz
        ohlcv_data: dict[tuple[str, str], list[OHLCVCandle]] = {}
        loop = asyncio.get_running_loop()

        try:
            from tqdm import tqdm
        except ImportError:  # pragma: no cover
            def tqdm(x=None, **_):  # type: ignore
                return x if x is not None else _Noop()

        async def _fetch_one(symbol: str) -> None:
            candles = await asyncio.to_thread(self._load_candles, symbol)
            if not candles or len(candles) < self._min_bars:
                return
            ohlcv_data[(symbol, "1d")] = candles
            if need_weekly:
                weekly = _derive_weekly_from_daily(candles, session_tz)
                if weekly:
                    ohlcv_data[(symbol, "1W")] = weekly

        await asyncio.gather(*[_fetch_one(s) for s in self._symbols])

        pattern_specs = [
            (type(p).__module__, type(p).__qualname__) for p in self._patterns
        ]
        config = self._config()
        max_workers = max(1, self._max_workers)
        total = len(tasks)
        completed = 0

        def _merge(res: tuple) -> None:
            nonlocal completed
            trades, signals, blocked, filtered = res
            result.trades.extend(trades)
            result.total_signals += signals
            result.blocked.extend(blocked)
            result.filtered.extend(filtered)
            completed += 1
            if self._progress_callback is not None:
                self._progress_callback(completed, total)

        try:
            ctx = mp.get_context("spawn")
            with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as pool:
                futures = [
                    loop.run_in_executor(
                        pool, _worker_symbol_backtest,
                        s, tf, pattern_specs, config, ohlcv_data.get((s, tf)),
                    )
                    for s, tf in tasks
                ]
                for coro in asyncio.as_completed(futures):
                    try:
                        _merge(await coro)
                    except Exception:
                        log.warning("Backtester | worker failed, skipping", exc_info=True)
                        completed += 1
        except Exception:
            log.warning("Backtester | pool unavailable — running inline", exc_info=True)
            for s, tf in tasks:
                candles = ohlcv_data.get((s, tf))
                _merge(_core_backtest_symbol(s, tf, candles or [], self._patterns, config))

        return result

    # ── Static wrappers kept for existing callers / tests ────────────────
    _open_trade = staticmethod(_open_trade)
    _close_trade = staticmethod(_close_trade)
    _check_exit = staticmethod(_check_exit)
    _make_snapshot = staticmethod(_make_snapshot)
    _update_neckline_state = staticmethod(_update_neckline_state)
    _update_trailing_reference = staticmethod(_update_trailing_reference)
    _trailing_stop_price = staticmethod(_trailing_stop_price)
    _min_required_bars = staticmethod(_min_required_bars)


class _Noop:
    def update(self, *_):  # pragma: no cover
        pass

    def close(self):  # pragma: no cover
        pass
