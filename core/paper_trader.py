"""
core/paper_trader.py — Live paper-trading account.

Runs the exact same scan → detect → manage-exit pipeline as the live
scanner and the backtester's trade-management logic (_open_trade,
_check_exit, _close_trade from core.backtester), but against a virtual
account instead of a real broker. No network/broker calls happen here —
this module only tracks fake cash, fake positions, and fake fills.

Persisted to a single JSON file so a session survives a restart.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.backtester import (
    BacktestResult,
    BacktestTrade,
    _apply_sizing,
    _check_exit,
    _close_trade,
    _open_trade,
    _update_trailing_reference,
    trade_r_multiple,
    trade_risk_dollars,
)
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal
from utils.logger import log

DEFAULT_ACCOUNT_PATH = Path("data/cache/paper_account.json")

# Re-exported under paper-trading-friendly names — same math as the
# backtester uses (a "current price" works whether the trade is still open
# or already closed), so live and backtested reports stay comparable.
r_multiple = trade_r_multiple
risk_dollars = trade_risk_dollars


def unrealized_pct(position: BacktestTrade, current_price: float) -> float:
    if position.entry_price <= 0:
        return 0.0
    if position.action == "SELL":
        return (position.entry_price - current_price) / position.entry_price * 100
    return (current_price - position.entry_price) / position.entry_price * 100


def days_held(position: BacktestTrade, as_of: datetime | None = None) -> float:
    end = as_of or position.exit_date or datetime.now(timezone.utc)
    return (end - position.entry_date).total_seconds() / 86400


def position_status(position: BacktestTrade) -> str:
    """Coarse open-position state for a dashboard status column — the exit
    mechanics (_check_exit) already decide what actually triggers; this
    just reflects which protective level is currently armed."""
    if position._breakeven_armed:
        return "BREAKEVEN"
    if position._trailing_activated:
        return "TRAILING"
    return "OPEN"


def _trade_to_dict(t: BacktestTrade) -> dict:
    d = asdict(t)
    d["entry_date"] = t.entry_date.isoformat()
    d["exit_date"] = t.exit_date.isoformat()
    return d


def _trade_from_dict(d: dict) -> BacktestTrade:
    d = dict(d)
    d["entry_date"] = datetime.fromisoformat(d["entry_date"])
    d["exit_date"] = datetime.fromisoformat(d["exit_date"])
    return BacktestTrade(**d)


class PaperAccount:
    """Virtual cash + positions ledger, keyed by symbol (one open trade per
    symbol at a time — same constraint the backtester and live scanner
    already assume)."""

    def __init__(
        self,
        initial_capital: float | None = None,
        txn_cost_pct: float = 0.0005,
        slippage_pct: float | None = None,
    ):
        self.initial_capital = initial_capital or settings.paper_initial_capital
        self.cash = self.initial_capital
        self.txn_cost_pct = txn_cost_pct
        self.slippage_pct = (
            slippage_pct if slippage_pct is not None else settings.paper_slippage_pct
        )
        self.positions: dict[str, BacktestTrade] = {}
        self.closed: list[BacktestTrade] = []
        self.equity_curve: list[tuple[str, float]] = []
        self._last_price: dict[str, float] = {}
        self._tick = 0
        # Per-symbol count of *actual new bars* seen (as opposed to
        # scan cycles) — scan_interval_seconds is typically much shorter
        # than a daily pattern's bar (e.g. hourly scans of a daily
        # timeframe), so counting scan cycles as "bars" would make
        # min_hold_bars arm in hours instead of days. Bumped from
        # MarketScanner via on_bar(..., is_new_bar=True).
        self._bar_count: dict[str, int] = {}
        self._daily_key = ""
        self._daily_pnl = 0.0
        # The scanner runs in a background thread with its own asyncio loop
        # (see ui/paper_dashboard.py) while the UI polls this same account
        # from the Tk main thread every second. Without a lock, the UI's
        # `for sym, p in self.positions.items()` can crash with "dictionary
        # changed size during iteration" the instant a scan opens/closes a
        # trade mid-refresh. RLock (not Lock) because open_position() calls
        # self.equity(), which also acquires it, on the same thread.
        self._lock = threading.RLock()

    # ── Equity / accounting ─────────────────────────────────────────────
    def last_price(self, symbol: str, default: float) -> float:
        return self._last_price.get(symbol, default)

    def equity(self) -> float:
        with self._lock:
            open_value = sum(
                self._last_price.get(sym, p.entry_price) * p.qty * (1 if p.action == "BUY" else -1)
                for sym, p in self.positions.items()
            )
            return self.cash + open_value

    def exposure(self) -> dict:
        """Long/short notional exposure across open positions, as a % of
        equity — e.g. six same-direction positions read as one large bet
        even if they're diversified across symbols."""
        with self._lock:
            equity = self.equity()
            long_value = sum(
                self._last_price.get(sym, p.entry_price) * p.qty
                for sym, p in self.positions.items() if p.action == "BUY"
            )
            short_value = sum(
                self._last_price.get(sym, p.entry_price) * p.qty
                for sym, p in self.positions.items() if p.action == "SELL"
            )
            if equity <= 0:
                return {"long_pct": 0.0, "short_pct": 0.0, "net_pct": 0.0}
            long_pct = long_value / equity * 100
            short_pct = short_value / equity * 100
            return {"long_pct": long_pct, "short_pct": short_pct, "net_pct": long_pct - short_pct}

    def positions_snapshot(self) -> list[tuple[str, BacktestTrade]]:
        """Thread-safe copy for callers (the UI) that iterate positions from
        a different thread than the one mutating them."""
        with self._lock:
            return list(self.positions.items())

    def closed_snapshot(self) -> list[BacktestTrade]:
        with self._lock:
            return list(self.closed)

    def equity_curve_snapshot(self) -> list[tuple[str, float]]:
        with self._lock:
            return list(self.equity_curve)

    def _reset_daily_if_needed(self, ts: datetime) -> None:
        key = ts.strftime("%Y-%m-%d")
        if key != self._daily_key:
            self._daily_key = key
            self._daily_pnl = 0.0

    def tick(self) -> None:
        """Call once per scan cycle (not per symbol) — drives min-hold-bar /
        trailing-activation timing the same way `bar_idx` does in the
        backtester's per-bar loop."""
        self._tick += 1

    # ── Signal → simulated fill ──────────────────────────────────────────
    def open_position(
        self,
        signal: TradeSignal,
        candle: OHLCVCandle,
        store: OHLCVStore,
    ) -> bool:
        self._reset_daily_if_needed(datetime.now(timezone.utc))

        with self._lock:
            return self._open_position_locked(signal, candle, store)

    def _open_position_locked(
        self, signal: TradeSignal, candle: OHLCVCandle, store: OHLCVStore,
    ) -> bool:
        if signal.symbol in self.positions:
            return False
        if len(self.positions) >= settings.max_open_positions:
            log.info("Paper | max_open_positions reached — skipping signal")
            return False
        if self._daily_pnl <= -settings.max_daily_loss_usd:
            log.info("Paper | daily loss limit hit — skipping signal")
            return False

        _apply_sizing(
            signal, store, signal.symbol, signal.timeframe,
            account_value=self.equity(),
            risk_per_trade_pct=0.02,
            position_sizing="risk",
            entry_price=candle.close,
            max_position_pct=0.10,
        )
        if candle.close > 0:
            max_qty = int(settings.max_position_size_usd / candle.close)
            signal.qty = max(1, min(int(signal.qty), max(1, max_qty)))

        fill_candle = candle
        slip = self.slippage_pct
        if slip:
            slipped_close = (
                candle.close * (1 + slip)
                if signal.action == "BUY"
                else candle.close * (1 - slip)
            )
            fill_candle = OHLCVCandle(
                open=candle.open, high=candle.high, low=candle.low,
                close=slipped_close, volume=candle.volume,
                timestamp=candle.timestamp,
            )

        position = _open_trade(signal, fill_candle, self._bar_count.get(signal.symbol, 0))
        # _open_trade stamps entry_date from the OHLCV bar's timestamp, which
        # is just the trading day (correct for the backtester replaying
        # history). A live paper fill needs the real wall-clock moment it
        # happened, so multiple fills on the same trading day are distinguishable.
        position.entry_date = datetime.now(timezone.utc)
        position.exit_date = position.entry_date
        notional = position.entry_price * position.qty
        if signal.action == "BUY":
            self.cash -= notional
        else:
            self.cash += notional  # short: receive proceeds up front

        self.positions[signal.symbol] = position
        self._last_price[signal.symbol] = position.entry_price
        log.info(
            f"Paper | OPEN {signal.action} {signal.qty} {signal.symbol} "
            f"@ {position.entry_price:.2f} (pattern={signal.pattern})"
        )
        return True

    # ── Per-bar update / exit check ──────────────────────────────────────
    def on_bar(
        self,
        symbol: str,
        candle: OHLCVCandle,
        timeframe: str | None = None,
        is_new_bar: bool = True,
    ) -> None:
        with self._lock:
            self._on_bar_locked(symbol, candle, timeframe, is_new_bar)

    def _on_bar_locked(
        self, symbol: str, candle: OHLCVCandle, timeframe: str | None, is_new_bar: bool,
    ) -> None:
        self._last_price[symbol] = candle.close
        if is_new_bar:
            self._bar_count[symbol] = self._bar_count.get(symbol, 0) + 1
        position = self.positions.get(symbol)
        if position is None:
            return
        # A symbol can be scanned on several timeframes per cycle (different
        # patterns watching different intervals). Only the candle matching
        # the position's own timeframe is valid for exit checks — e.g. a
        # weekly candle's high/low would spuriously trip a stop set from a
        # daily entry.
        if timeframe is not None and timeframe != position.timeframe:
            return
        now = datetime.now(timezone.utc)
        self._reset_daily_if_needed(now)

        # Match the backtester's canonical min_hold_bars=2 (see main.py) so
        # trailing/breakeven don't arm a bar earlier here than in backtests —
        # otherwise "live" and backtested results for the same pattern diverge.
        # bar_idx is counted per real new bar (see _bar_count), not per scan
        # cycle, since scans typically run far more often than a new daily
        # bar forms.
        bar_idx = self._bar_count.get(symbol, 0)
        exit_price, reason = _check_exit(candle, position, bar_idx, min_hold_bars=2)
        if exit_price is None:
            _update_trailing_reference(position, candle)
            return

        _close_trade(position, exit_price, reason, candle, self.txn_cost_pct)
        position.exit_date = datetime.now(timezone.utc)  # real fill time, not bar date
        notional_out = exit_price * position.qty
        # Commission cost mirrors _close_trade's pnl deduction (entry + exit
        # legs) so cash/equity stay in lockstep with the reported trade pnl —
        # otherwise cash silently overstates the account by the cost drag.
        cost = (position.entry_price * self.txn_cost_pct + exit_price * self.txn_cost_pct) * position.qty
        if position.action == "BUY":
            self.cash += notional_out - cost
        else:
            self.cash -= notional_out + cost  # buy back the short, plus commission

        self._daily_pnl += position.pnl * position.qty
        del self.positions[symbol]
        self.closed.append(position)
        self.equity_curve.append((now.isoformat(), self.equity()))
        log.info(
            f"Paper | CLOSE {symbol} reason={reason} pnl={position.pnl_pct:+.2f}%"
        )

    # ── Reporting ─────────────────────────────────────────────────────────
    def to_result(self) -> BacktestResult:
        with self._lock:
            return BacktestResult(
                trades=list(self.closed),
                total_signals=len(self.closed) + len(self.positions),
                initial_capital=self.initial_capital,
            )

    # ── Persistence ───────────────────────────────────────────────────────
    def save(self, path: str | Path = DEFAULT_ACCOUNT_PATH) -> None:
        with self._lock:
            payload = {
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "tick": self._tick,
                "bar_count": self._bar_count,
                "daily_key": self._daily_key,
                "daily_pnl": self._daily_pnl,
                "positions": {
                    sym: _trade_to_dict(t) for sym, t in self.positions.items()
                },
                "closed": [_trade_to_dict(t) for t in self.closed],
                "equity_curve": list(self.equity_curve),
            }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_ACCOUNT_PATH) -> "PaperAccount":
        p = Path(path)
        acct = cls()
        if not p.exists():
            return acct
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning(f"Paper | failed to load {p}, starting fresh")
            return acct
        acct.initial_capital = data.get("initial_capital", acct.initial_capital)
        acct.cash = data.get("cash", acct.initial_capital)
        acct._tick = data.get("tick", 0)
        acct._bar_count = data.get("bar_count", {})
        acct._daily_key = data.get("daily_key", "")
        acct._daily_pnl = data.get("daily_pnl", 0.0)
        acct.positions = {
            sym: _trade_from_dict(d) for sym, d in data.get("positions", {}).items()
        }
        acct.closed = [_trade_from_dict(d) for d in data.get("closed", [])]
        acct.equity_curve = [tuple(x) for x in data.get("equity_curve", [])]
        acct._last_price = {sym: t.entry_price for sym, t in acct.positions.items()}
        return acct
