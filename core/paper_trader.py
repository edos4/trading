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
from zoneinfo import ZoneInfo

from config import settings
from core.backtester import (
    BacktestResult,
    BacktestTrade,
    _apply_notional_sizing,
    _check_exit,
    _close_trade,
    _open_trade,
    trade_r_multiple,
    trade_risk_dollars,
)
from core.engine_defaults import ENGINE, is_fractional_qty
from core.market import (
    apply_lot_rounding,
    format_money,
    get_market,
    may_assume_fill,
    session_label,
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
    """as_of (a wall-clock "now") is only ever passed for still-open
    positions, so entry_date (also wall-clock) is the right start there. A
    closed trade has no as_of — use sim_entry_date/sim_exit_date, the
    underlying bar timestamps, so a stream-replay run reports simulated days
    held instead of the real minutes/seconds the replay took to run."""
    if as_of is not None:
        return (as_of - position.entry_date).total_seconds() / 86400
    start = position.sim_entry_date or position.entry_date
    end = position.sim_exit_date or position.exit_date or datetime.now(timezone.utc)
    return (end - start).total_seconds() / 86400


def sim_days_held(
    position: BacktestTrade,
    as_of: datetime | None = None,
) -> float:
    """Elapsed holding time in simulated market days."""
    start = position.sim_entry_date or position.entry_date
    end = (
        position.sim_exit_date or position.exit_date or datetime.now(timezone.utc)
        if as_of is None else as_of
    )
    return max(0.0, (end - start).total_seconds() / 86400)


def bars_held(position: BacktestTrade, current_bar_idx: int | None = None) -> int | None:
    if position.entry_bar_idx < 0:
        return None
    end_idx = position.exit_bar_idx if current_bar_idx is None else current_bar_idx
    if end_idx is None:
        return None
    return max(0, end_idx - position.entry_bar_idx)


def position_status(position: BacktestTrade) -> str:
    """Coarse open-position state for a dashboard status column."""
    if position._trailing_activated:
        return "TRAILING"
    return "OPEN"


def _trade_to_dict(t: BacktestTrade) -> dict:
    d = asdict(t)
    d["entry_date"] = t.entry_date.isoformat()
    d["exit_date"] = t.exit_date.isoformat()
    d["sim_entry_date"] = t.sim_entry_date.isoformat() if t.sim_entry_date else None
    d["sim_exit_date"] = t.sim_exit_date.isoformat() if t.sim_exit_date else None
    return d


def _trade_from_dict(d: dict) -> BacktestTrade:
    import dataclasses

    d = dict(d)
    d["entry_date"] = datetime.fromisoformat(d["entry_date"])
    d["exit_date"] = datetime.fromisoformat(d["exit_date"])
    if d.get("sim_entry_date"):
        d["sim_entry_date"] = datetime.fromisoformat(d["sim_entry_date"])
    if d.get("sim_exit_date"):
        d["sim_exit_date"] = datetime.fromisoformat(d["sim_exit_date"])
    if d.get("position_marks") is None:
        d["position_marks"] = []
    if isinstance(d.get("reclaim_lower_rail"), list):
        d["reclaim_lower_rail"] = tuple(d["reclaim_lower_rail"])
    # Tolerate ledgers saved under the old (pre-refactor) BacktestTrade schema.
    known = {f.name for f in dataclasses.fields(BacktestTrade)}
    return BacktestTrade(**{k: v for k, v in d.items() if k in known})


def _position_mark_row(
    position: BacktestTrade,
    *,
    price: float,
    as_of: datetime,
    bar_idx: int | None,
    session_date: str,
    status: str | None = None,
) -> dict:
    if position.action == "BUY":
        mtm = (price - position.entry_price) * position.qty
    else:
        mtm = (position.entry_price - price) * position.qty
    r_val = r_multiple(position, price)
    return {
        "date": session_date,
        "sim_bar": as_of.isoformat(),
        "close": price,
        "unrl_pct": unrealized_pct(position, price),
        "mtm": mtm,
        "r": r_val,
        "value": price * position.qty,
        "status": status if status is not None else position_status(position),
        "bars": bars_held(position, bar_idx),
        "stop": position.stop_loss,
        "target": position.take_profit,
    }


class PaperAccount:
    """Virtual cash + positions ledger, keyed by symbol (one open trade per
    symbol at a time — same constraint the backtester and live scanner
    already assume)."""

    def __init__(
        self,
        initial_capital: float | None = None,
        txn_cost_pct: float | None = None,
        slippage_pct: float | None = None,
        market: str | None = None,
        max_daily_loss: float | None = None,
    ):
        self.market = get_market(market).id
        profile = get_market(self.market)
        if initial_capital is not None:
            self.initial_capital = initial_capital
        elif profile.id == "ph":
            self.initial_capital = profile.paper_initial_capital
        else:
            self.initial_capital = settings.paper_initial_capital
        self.cash = self.initial_capital
        self.txn_cost_pct = (
            profile.txn_cost_pct if txn_cost_pct is None else txn_cost_pct
        )
        # Slippage defaults OFF (backtest/paper parity, `.cjs` methodology).
        # Set > 0 explicitly to model fill slippage on the paper book.
        self.slippage_pct = slippage_pct if slippage_pct is not None else 0.0
        # Vestigial — no daily-loss limit is enforced anymore.
        self.max_daily_loss = float("inf") if max_daily_loss is None else max_daily_loss
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
        # New-bar counters are keyed by symbol + timeframe. A symbol can be
        # scanned on both 1d and 1W; sharing one counter makes a weekly candle
        # advance a daily position's bar clock and can trigger time-stops,
        # cooldowns, and trailing logic at the wrong time.
        self._bar_count: dict[str, int] = {}
        self._daily_key = ""
        self._daily_pnl = 0.0
        # Stable per-symbol/timeframe identities of the last fully processed
        # swing bar. Persisting these prevents a clean restart from replaying
        # the same daily/weekly bar and incrementing bar counters twice.
        self._processed_bar_ids: dict[str, str] = {}
        # Latest market/bar timestamp observed by the paper account. This is
        # the clock used by the UI for simulated age during replay.
        self._sim_now: datetime | None = None
        # Latest simulated timestamp per timeframe. The old single global
        # clock could age a daily position using a different timeframe's
        # latest candle, producing nonsensical open-position ages.
        self._sim_now_by_timeframe: dict[str, datetime] = {}
        # Historical paper stream replays closed daily bars after hours.
        # Wall-clock PSE AM/PM must not block those assumed fills.
        self.assume_session_open = False
        # Session flag: not persisted. Scanner sets this from Pattern-only.
        self.pattern_only = False
        # The scanner runs in a background thread with its own asyncio loop
        # (see ui/paper_dashboard.py) while the UI polls this same account
        # from the Tk main thread every second. Without a lock, the UI's
        # `for sym, p in self.positions.items()` can crash with "dictionary
        # changed size during iteration" the instant a scan opens/closes a
        # trade mid-refresh. RLock (not Lock) because open_position() calls
        # self.equity(), which also acquires it, on the same thread.
        self._lock = threading.RLock()

    def _record_position_mark(
        self,
        symbol: str,
        position: BacktestTrade,
        price: float,
        as_of: datetime,
        bar_idx: int | None,
        status: str | None = None,
    ) -> None:
        """Append or refresh one session mark for an open position."""
        ts = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(get_market(self.market).session_tz)
        session_date = ts.astimezone(tz).strftime("%Y-%m-%d")
        mark = _position_mark_row(
            position,
            price=price,
            as_of=ts,
            bar_idx=bar_idx,
            session_date=session_date,
            status=status,
        )
        marks = position.position_marks
        if marks and marks[-1].get("date") == session_date:
            marks[-1] = mark
        else:
            marks.append(mark)
        r_txt = f"{mark['r']:.2f}" if mark["r"] is not None else "—"
        bars_txt = mark["bars"] if mark["bars"] is not None else "—"
        log.info(
            f"Paper | MARK {symbol} {session_date} close={price:.4f} "
            f"unrl={mark['unrl_pct']:+.2f}% "
            f"mtm={format_money(mark['mtm'], self.market, signed=True)} "
            f"r={r_txt} status={mark['status']} bars={bars_txt} "
            f"value={format_money(mark['value'], self.market)}"
        )

    # ── Equity / accounting ─────────────────────────────────────────────
    def last_price(self, symbol: str, default: float) -> float:
        return self._last_price.get(symbol, default)

    def bar_count(self, symbol: str, timeframe: str | None = None) -> int:
        """New-bar count for a symbol/timeframe.

        The timeframe is part of the clock because 1d and 1W bars must not
        advance the same counter. `timeframe=None` is retained as a backwards-
        compatible fallback for older callers/accounts.
        """
        if timeframe:
            return self._bar_count.get(f"{symbol}|{timeframe}", 0)
        exact = self._bar_count.get(symbol)
        if exact is not None:
            return exact
        # Backwards-compatible convenience: if there is exactly one counter
        # for this symbol, return it rather than silently returning zero.
        prefix = f"{symbol}|"
        matches = [
            value for key, value in self._bar_count.items()
            if key.startswith(prefix)
        ]
        return matches[0] if len(matches) == 1 else 0

    def processed_bar_identities_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._processed_bar_ids)

    def mark_bar_processed(self, symbol: str, timeframe: str, identity_key: str) -> None:
        with self._lock:
            self._processed_bar_ids[f"{symbol}|{timeframe}"] = identity_key

    def sim_now(self, timeframe: str | None = None) -> datetime | None:
        with self._lock:
            if timeframe:
                return self._sim_now_by_timeframe.get(timeframe)
            return self._sim_now

    def equity(self) -> float:
        with self._lock:
            # Longs contribute positive market value; shorts contribute
            # negative (a short is a liability — the shares must be bought
            # back). Because opening a short also credits cash by its sale
            # proceeds, the two cancel at entry and only P&L moves equity.
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
                return {
                    "long_pct": 0.0, "short_pct": 0.0,
                    "net_pct": 0.0, "gross_pct": 0.0,
                }
            long_pct = long_value / equity * 100
            short_pct = short_value / equity * 100
            gross_pct = (long_value + short_value) / equity * 100
            return {
                "long_pct": long_pct,
                "short_pct": short_pct,
                "net_pct": long_pct - short_pct,
                "gross_pct": gross_pct,
            }

    def snapshot_metrics(self) -> dict:
        """Atomic, self-consistent read of cash/positions/prices.

        Callers previously assembled a dashboard snapshot by calling
        `equity()`, `positions_snapshot()`, `last_price()` (per symbol),
        `exposure()`, `realized_pnl_dollars()`, and `unrealized_pnl_dollars()`
        as separate calls. Each of those independently acquires and releases
        `self._lock`, so a concurrent scanner thread (which mutates cash,
        positions, and `_last_price` inside `open_position`/`on_bar`, also
        under this same lock) can run *between* those calls. During a live
        stream that reprices positions continuously, this let the per-position
        MTM figures used to build the "Value"/"MTM" table rows be computed
        against a different price snapshot than the account-level
        `unrealized_pnl_dollars()`/`equity()` totals shown in the header,
        so the header total silently drifted from the sum of the visible
        rows. Taking the lock exactly once here and deriving every figure
        from that single frozen read keeps them consistent with each other.
        """
        with self._lock:
            cash = self.cash
            initial_capital = self.initial_capital
            positions = dict(self.positions)
            last_price = dict(self._last_price)
            closed = list(self.closed)

        long_value = 0.0
        short_value = 0.0
        unrealized = 0.0
        rows: list[tuple[str, BacktestTrade, float, float]] = []
        for sym, p in positions.items():
            current = last_price.get(sym, p.entry_price)
            if p.action == "BUY":
                mtm = (current - p.entry_price) * p.qty
                long_value += current * p.qty
            else:
                mtm = (p.entry_price - current) * p.qty
                short_value += current * p.qty
            unrealized += mtm
            rows.append((sym, p, current, mtm))

        open_value = long_value - short_value
        equity = cash + open_value
        realized = sum(t.pnl * t.qty for t in closed)
        total_pnl = equity - initial_capital

        if equity > 0:
            long_pct = long_value / equity * 100
            short_pct = short_value / equity * 100
            exposure = {
                "long_pct": long_pct,
                "short_pct": short_pct,
                "net_pct": long_pct - short_pct,
                "gross_pct": (long_value + short_value) / equity * 100,
            }
        else:
            exposure = {
                "long_pct": 0.0, "short_pct": 0.0,
                "net_pct": 0.0, "gross_pct": 0.0,
            }

        return {
            "cash": cash,
            "initial_capital": initial_capital,
            "equity": equity,
            "exposure": exposure,
            "realized_pnl_dollars": realized,
            "unrealized_pnl_dollars": unrealized,
            "total_pnl_dollars": total_pnl,
            "positions": rows,  # [(symbol, BacktestTrade, current_price, mtm), ...]
            "closed": closed,
        }

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
        profile = get_market(self.market)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(ZoneInfo(profile.session_tz))
        key = local.strftime("%Y-%m-%d")
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
    ) -> tuple[bool, str]:
        self._reset_daily_if_needed(candle.timestamp or datetime.now(timezone.utc))

        with self._lock:
            return self._open_position_locked(signal, candle, store)

    def _open_position_locked(
        self, signal: TradeSignal, candle: OHLCVCandle, store: OHLCVStore,
    ) -> tuple[bool, str]:
        if signal.symbol in self.positions:
            return False, (
                f"Already flat-blocked: an open position exists in {signal.symbol}, "
                f"so a second concurrent entry was skipped."
            )
        profile = get_market(self.market)
        # PH is long-only (retail shorts need SBL); every other constraint the
        # engine used to enforce (portfolio caps, daily loss, gross exposure,
        # cash affordability, R:R) is gone — trades are independent flat $10k.
        if profile.long_only and signal.action == "SELL":
            return False, (
                f"Long-only {profile.label}: SELL/short signals are disabled."
            )
        if not self.assume_session_open and not may_assume_fill(self.market):
            window = session_label(self.market)
            return False, (
                f"Session {window}: paper will not assume a fill outside continuous "
                f"AM/PM matching."
            )

        signal.price = candle.close
        _apply_notional_sizing(
            signal, ENGINE.position_notional,
            fractional=is_fractional_qty(signal.pattern),
        )
        if signal.qty <= 0 or (
            signal.qty < 1 and not is_fractional_qty(signal.pattern)
        ):
            return False, (
                f"Sizing: ${ENGINE.position_notional:,.0f} buys < 1 share of "
                f"{signal.symbol} at {format_money(candle.close, self.market)}."
            )
        if profile.lot_round and not apply_lot_rounding(signal):
            return False, (
                f"Lot rounding: {signal.symbol} size rounded below one board lot."
            )

        fill_candle = candle
        slip = self.slippage_pct
        if slip:
            slipped_close = (
                candle.close * (1 + slip) if signal.action == "BUY"
                else candle.close * (1 - slip)
            )
            fill_candle = OHLCVCandle(
                open=candle.open, high=candle.high, low=candle.low,
                close=slipped_close, volume=candle.volume, timestamp=candle.timestamp,
            )

        position = _open_trade(
            signal, fill_candle, self.bar_count(signal.symbol, signal.timeframe),
        )
        # _open_trade stamps entry_date from the bar timestamp; a live paper
        # fill needs the real wall-clock moment. Keep the bar time in
        # sim_entry_date so days_held() still reports simulated holding time.
        position.sim_entry_date = position.entry_date
        position.entry_date = datetime.now(timezone.utc)
        position.exit_date = position.entry_date
        notional = position.entry_price * position.qty
        if signal.action == "BUY":
            self.cash -= notional
        else:
            self.cash += notional  # short: receive proceeds up front

        self.positions[signal.symbol] = position
        self._last_price[signal.symbol] = position.entry_price
        mark_ts = position.sim_entry_date or fill_candle.timestamp or datetime.now(timezone.utc)
        self._record_position_mark(
            signal.symbol,
            position,
            position.entry_price,
            mark_ts,
            self.bar_count(signal.symbol, signal.timeframe),
        )
        log.info(
            f"Paper | OPEN {signal.action} {signal.qty} {signal.symbol} "
            f"@ {position.entry_price:.2f} (pattern={signal.pattern})"
        )
        return True, (
            f"Filled signal-bar entry: {signal.action} {signal.qty:g} {signal.symbol} "
            f"@ {format_money(position.entry_price, self.market)} (pattern={signal.pattern})."
        )

    # ── Per-bar update / exit check ──────────────────────────────────────
    def on_bar(
        self,
        symbol: str,
        candle: OHLCVCandle,
        timeframe: str | None = None,
        is_new_bar: bool = True,
    ) -> BacktestTrade | None:
        """Update marks / exits. Returns the closed trade if this bar exited."""
        with self._lock:
            return self._on_bar_locked(symbol, candle, timeframe, is_new_bar)

    def _on_bar_locked(
        self, symbol: str, candle: OHLCVCandle, timeframe: str | None, is_new_bar: bool,
    ) -> BacktestTrade | None:
        self._last_price[symbol] = candle.close
        if candle.timestamp is not None:
            if self._sim_now is None or candle.timestamp > self._sim_now:
                self._sim_now = candle.timestamp
            tf_key = timeframe or "1d"
            previous_tf = self._sim_now_by_timeframe.get(tf_key)
            if previous_tf is None or candle.timestamp > previous_tf:
                self._sim_now_by_timeframe[tf_key] = candle.timestamp
        if not is_new_bar:
            return None

        tf = timeframe or "1d"
        counter_key = f"{symbol}|{tf}"
        self._bar_count[counter_key] = self._bar_count.get(counter_key, 0) + 1
        position = self.positions.get(symbol)
        if position is None:
            return None
        # A symbol can be scanned on several timeframes per cycle (different
        # patterns watching different intervals). Only the candle matching
        # the position's own timeframe is valid for exit checks — e.g. a
        # weekly candle's high/low would spuriously trip a stop set from a
        # daily entry.
        if timeframe is not None and timeframe != position.timeframe:
            return None
        now = candle.timestamp or datetime.now(timezone.utc)
        self._reset_daily_if_needed(now)
        bar_idx = self.bar_count(symbol, position.timeframe)

        # bar_idx is per real new bar (see _bar_count), not per scan cycle, so
        # the exit ladder's bar counting matches the backtester's walk index.
        from core.backtester import _update_neckline_state, _update_prev_hl
        _update_neckline_state(position, candle, bar_idx)
        exit_price, reason = _check_exit(candle, position, bar_idx)
        if exit_price is None:
            _update_prev_hl(position, candle)
        if reason == "time_exit":
            elapsed = position.time_exit_bars_elapsed
            configured = position.exit_bars_after_neckline_break
            signal_or_break_idx = position.neckline_break_bar_idx
            position_bars = (
                bar_idx - position.entry_bar_idx
                if position.entry_bar_idx >= 0 else None
            )
            log.info(
                f"Paper | TIME_EXIT {symbol} | "
                f"elapsed={elapsed} bars from breakout/signal "
                f"(configured={configured}) | "
                f"entry_bar={position.entry_bar_idx} "
                f"breakout_bar={signal_or_break_idx} "
                f"exit_bar={bar_idx} "
                f"position_bars={position_bars}"
            )
        if exit_price is not None and self.slippage_pct:
            # Exit is a sell (closing a BUY) or a buy-to-cover (closing a
            # SELL) — same slippage direction logic as the entry fill, so
            # exits aren't priced better than a real fill would be.
            exit_price = (
                exit_price * (1 - self.slippage_pct)
                if position.action == "BUY"
                else exit_price * (1 + self.slippage_pct)
            )
        # Mark after the exit check so the last row is the fill, not a
        # close that can still look green on a bar that stopped out.
        mark_price = exit_price if exit_price is not None else candle.close
        self._record_position_mark(
            symbol, position, mark_price, now, bar_idx,
            status=reason if exit_price is not None else None,
        )
        if exit_price is None:
            return None

        _close_trade(position, exit_price, reason, candle, self.txn_cost_pct)
        position.sim_exit_date = position.exit_date  # bar timestamp, for days_held()
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
        self.mark_to_market(now)
        log.info(
            f"Paper | CLOSE {symbol} reason={reason} pnl={position.pnl_pct:+.2f}%"
        )
        return position

    def mark_to_market(self, as_of: datetime | None = None) -> float:
        """Record one portfolio equity mark per market session date.

        The performance engine treats these marks as daily returns. Recording
        one point per scan (e.g. every 60 seconds) would make Sharpe depend on
        the scanner interval and be meaningless during compressed replay.
        """
        with self._lock:
            ts = as_of or self._sim_now or datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            tz = ZoneInfo(get_market(self.market).session_tz)
            period_key = ts.astimezone(tz).strftime("%Y-%m-%d")
            value = self.equity()
            point = (ts.isoformat(), value)
            if self.equity_curve:
                try:
                    last_ts = datetime.fromisoformat(self.equity_curve[-1][0])
                    last_key = last_ts.astimezone(tz).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    last_key = None
                if last_key == period_key:
                    self.equity_curve[-1] = point
                    return value
            self.equity_curve.append(point)
            return value

    def realized_pnl_dollars(self) -> float:
        with self._lock:
            return sum(t.pnl * t.qty for t in self.closed)

    def unrealized_pnl_dollars(self) -> float:
        with self._lock:
            return sum(
                (
                    self._last_price.get(sym, p.entry_price) - p.entry_price
                    if p.action == "BUY"
                    else p.entry_price - self._last_price.get(sym, p.entry_price)
                ) * p.qty
                for sym, p in self.positions.items()
            )

    # ── Reporting ─────────────────────────────────────────────────────────
    def to_result(self) -> BacktestResult:
        with self._lock:
            return BacktestResult(
                trades=list(self.closed),
                total_signals=len(self.closed) + len(self.positions),
                position_notional=ENGINE.position_notional,
                version=f"paper/{self.market}",
            )

    # ── Persistence ───────────────────────────────────────────────────────
    def save(self, path: str | Path | None = None) -> None:
        with self._lock:
            payload = {
                "market": self.market,
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "txn_cost_pct": self.txn_cost_pct,
                "max_daily_loss": self.max_daily_loss,
                "tick": self._tick,
                "bar_count": self._bar_count,
                "sim_now_by_timeframe": {
                    tf: ts.isoformat() for tf, ts in self._sim_now_by_timeframe.items()
                },
                "daily_key": self._daily_key,
                "daily_pnl": self._daily_pnl,
                "processed_bar_ids": dict(self._processed_bar_ids),
                "sim_now": self._sim_now.isoformat() if self._sim_now else None,
                "last_price": dict(self._last_price),
                "positions": {
                    sym: _trade_to_dict(t) for sym, t in self.positions.items()
                },
                "closed": [_trade_to_dict(t) for t in self.closed],
                "equity_curve": list(self.equity_curve),
            }
        p = Path(path) if path is not None else get_market(self.market).paper_account_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path | None = None, *, market: str | None = None) -> "PaperAccount":
        profile = get_market(market)
        p = Path(path) if path is not None else profile.paper_account_path
        acct = cls(market=profile.id)
        if not p.exists():
            return acct
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning(f"Paper | failed to load {p}, starting fresh")
            return acct
        saved_market = data.get("market")
        if saved_market and get_market(saved_market).id != profile.id:
            log.warning(
                f"Paper | {p} is a {saved_market} ledger; starting a fresh "
                f"{profile.id} account instead of mixing books"
            )
            return acct
        acct.initial_capital = data.get("initial_capital", acct.initial_capital)
        acct.cash = data.get("cash", acct.initial_capital)
        acct.txn_cost_pct = data.get("txn_cost_pct", acct.txn_cost_pct)
        acct.max_daily_loss = data.get("max_daily_loss", acct.max_daily_loss)
        acct._tick = data.get("tick", 0)
        raw_bar_count = data.get("bar_count", {}) or {}
        # Migrate pre-timeframe accounts whose counters were keyed only by
        # symbol. Those counters represented the daily paper clock.
        acct._bar_count = {}
        for key, value in raw_bar_count.items():
            key = str(key)
            if "|" in key:
                acct._bar_count[key] = int(value)
            else:
                acct._bar_count[f"{key}|1d"] = int(value)
        acct._sim_now_by_timeframe = {}
        for tf, raw_ts in (data.get("sim_now_by_timeframe") or {}).items():
            try:
                acct._sim_now_by_timeframe[str(tf)] = datetime.fromisoformat(raw_ts)
            except (TypeError, ValueError):
                continue
        acct._daily_key = data.get("daily_key", "")
        acct._daily_pnl = data.get("daily_pnl", 0.0)
        acct._processed_bar_ids = {
            str(k): str(v) for k, v in (data.get("processed_bar_ids") or {}).items()
        }
        if data.get("sim_now"):
            try:
                acct._sim_now = datetime.fromisoformat(data["sim_now"])
            except (TypeError, ValueError):
                acct._sim_now = None
        acct.positions = {
            sym: _trade_from_dict(d) for sym, d in data.get("positions", {}).items()
        }
        acct.closed = [_trade_from_dict(d) for d in data.get("closed", [])]
        raw_curve = [tuple(x) for x in data.get("equity_curve", [])]
        # Migrate older files that stored one mark per scan. Keep the latest
        # mark for each market session so historical Sharpe is not contaminated
        # by the old polling frequency.
        tz = ZoneInfo(profile.session_tz)
        deduped_curve: list[tuple[str, float]] = []
        seen_keys: set[str] = set()
        for ts_raw, value in raw_curve:
            try:
                ts = datetime.fromisoformat(ts_raw)
                key = ts.astimezone(tz).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if key in seen_keys:
                for i in range(len(deduped_curve) - 1, -1, -1):
                    try:
                        old_ts = datetime.fromisoformat(deduped_curve[i][0])
                        if old_ts.astimezone(tz).strftime("%Y-%m-%d") == key:
                            deduped_curve[i] = (ts.isoformat(), float(value))
                            break
                    except (TypeError, ValueError):
                        continue
            else:
                deduped_curve.append((ts.isoformat(), float(value)))
                seen_keys.add(key)
        acct.equity_curve = deduped_curve
        # Prefer persisted marks so equity/MTM survive process restarts.
        # Fall back to entry (flat unrealized) for older account files that
        # never stored last_price.
        saved_marks = data.get("last_price") or {}
        acct._last_price = {
            sym: float(saved_marks.get(sym, t.entry_price))
            for sym, t in acct.positions.items()
        }
        return acct
