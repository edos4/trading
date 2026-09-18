"""Isolated historical-stream replay shared with paper trading's execution path.

The replay advances one simulated session at a time and drives the *same*
trade-management functions the paper account uses — ``_open_trade`` /
``_check_exit`` / ``_close_trade`` / ``_update_neckline_state`` from
``core.backtester``, applied in the same order as ``PaperAccount.on_bar``
(manage exits, then drain this bar's signals). It does not reimplement fills,
quantities, exit ladders or accounting.

Differences from the offline walk are deliberate and required by P06:

* the candle store grows causally — only bars at or before the simulated
  timestamp are visible, so future bars cannot influence earlier signals;
* each replay owns its account, dedup state, clock, stores and outputs, so it
  can never touch a running paper session;
* pre-start sessions only build indicators (no entries);
* the end policy is explicit (retain open positions by default, or force-close
  with labeled exits).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from core.backtester import (
    BacktestTrade,
    _apply_notional_sizing,
    _check_exit,
    _close_trade,
    _make_snapshot,
    _min_required_bars,
    _open_trade,
    _update_neckline_state,
    _update_prev_hl,
)
from core.engine_defaults import is_fractional_qty
from core.market import apply_lot_rounding
from core.pattern_edit_store import EditError
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import OHLCVCandle


class ReplayCancelled(EditError):
    pass


# patterns._dedup is a process-global registry (the same reason the live scanner
# runs detector analysis in spawned workers). Concurrent replays in one process
# must not interleave their registry swaps, so the analysis section is
# serialized here. True parallelism needs one process per replay — the durable
# job layer permits that; see docs/pattern-editor-stream.md.
_ANALYZE_LOCK = threading.Lock()


def rows_to_candles(rows: list[list]) -> list[OHLCVCandle]:
    """Decode the frozen dataset blob (``[[iso, o, h, l, c, v], ...]``)."""
    out: list[OHLCVCandle] = []
    for row in rows:
        stamp, open_, high, low, close, volume = row
        ts = datetime.fromisoformat(stamp) if stamp else None
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(OHLCVCandle(open=float(open_), high=float(high), low=float(low),
                               close=float(close), volume=float(volume), timestamp=ts))
    return out


def _session_date(candle: OHLCVCandle, session_tz: str) -> date:
    from zoneinfo import ZoneInfo

    ts = candle.timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ZoneInfo(session_tz)).date()


@dataclass
class _SymbolSeries:
    symbol: str
    candles: list[OHLCVCandle]
    by_date: dict[date, list[int]] = field(default_factory=dict)
    store: OHLCVStore | None = None
    pushed: int = 0
    used: set[int] = field(default_factory=set)
    open_positions: list[BacktestTrade] = field(default_factory=list)
    closed: list[BacktestTrade] = field(default_factory=list)
    last_close: float | None = None
    last_date: date | None = None


class StreamReplay:
    """One isolated, causal, session-at-a-time historical replay."""

    def __init__(self, *, dataset: dict[str, list[list]], patterns, settings,
                 session_tz: str, cancel=None, progress=None):
        if settings.mode != "historical-stream":
            raise EditError("StreamReplay requires a historical-stream preset")
        if settings.window is None:
            raise EditError("Historical stream requires a replay window")
        self.settings = settings
        self.window = settings.window
        self.session_tz = session_tz
        self.cancel = cancel
        self.progress = progress
        if settings.execution.sizing_mode != "fixed-notional":
            raise EditError("Only fixed-notional sizing is supported for stream replay")
        self.notional = settings.execution.position_notional
        self.txn_cost = settings.execution.txn_cost_pct
        self.lot_round = settings.market == "ph"
        self.patterns = list(patterns)
        self.series = self._build_series(dataset)
        if not self.series:
            raise EditError("No frozen daily history available for the selected symbols")
        self.events: list[dict] = []
        self.total_signals = 0
        self._closed_order: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []

    # ── setup ────────────────────────────────────────────────────────────
    def _build_series(self, dataset: dict[str, list[list]]) -> dict[str, _SymbolSeries]:
        series: dict[str, _SymbolSeries] = {}
        for symbol, rows in sorted(dataset.items()):
            candles = [c for c in rows_to_candles(rows) if c.timestamp is not None]
            if not candles:
                continue
            deduped: list[OHLCVCandle] = []
            for candle in candles:
                if deduped and candle.timestamp == deduped[-1].timestamp:
                    deduped[-1] = candle
                    self.events.append({"symbol": symbol, "status": "duplicate-bar",
                                        "session": candle.timestamp.isoformat()})
                elif deduped and candle.timestamp < deduped[-1].timestamp:
                    self.events.append({"symbol": symbol, "status": "unordered-bar",
                                        "session": candle.timestamp.isoformat()})
                    continue
                else:
                    deduped.append(candle)
            entry = _SymbolSeries(symbol=symbol, candles=deduped)
            for index, candle in enumerate(deduped):
                entry.by_date.setdefault(_session_date(candle, self.session_tz), []).append(index)
            series[symbol] = entry
        return series

    def _required_warmup(self) -> int:
        needed = [_min_required_bars(self.settings.timeframe), int(self.window.warmup_bars)]
        for pattern in self.patterns:
            if self.settings.timeframe in pattern.timeframes:
                needed.append(int(getattr(pattern, "MIN_BARS", 2) or 2))
        return max(needed)

    def _bounds(self) -> tuple[date, date | None]:
        return self.window.start_date, self.window.end_date

    # ── calendar ─────────────────────────────────────────────────────────
    def _calendar(self) -> list[date]:
        start, end = self._bounds()
        warm = self._required_warmup()
        days: set[date] = set()
        for series in self.series.values():
            first_entry = next((d for d in sorted(series.by_date) if d >= start), None)
            if first_entry is None:
                self.events.append({"symbol": series.symbol, "status": "history-ends-before-start"})
                continue
            before = [d for d in sorted(series.by_date) if d < first_entry]
            days.update(before[-warm:] if warm else [])
            days.update(d for d in series.by_date if d >= start)
        ordered = sorted(days)
        if end is not None:
            ordered = [d for d in ordered if d <= end]
        elif self.window.session_count is not None:
            tradable = [d for d in ordered if d >= start]
            if len(tradable) > self.window.session_count:
                cutoff = tradable[self.window.session_count - 1]
                ordered = [d for d in ordered if d <= cutoff]
        return ordered

    # ── execution ────────────────────────────────────────────────────────
    def run(self) -> dict:
        start, end = self._bounds()
        calendar = self._calendar()
        if not calendar:
            raise EditError("Replay window selects no sessions for the frozen history")
        first_tradable = next((d for d in calendar if d >= start), None)
        if first_tradable is None:
            raise EditError("Replay window starts after the frozen history ends")
        total = len(calendar)
        for index, session in enumerate(calendar):
            if self.cancel is not None and self.cancel.is_set():
                raise ReplayCancelled("Replay cancelled")
            self._advance_session(session, start)
            self._mark_equity(session)
            if self.progress is not None:
                self.progress(index + 1, total)
        if self.window.end_date is not None and calendar[-1] < self.window.end_date:
            self.events.append({"status": "end-of-data",
                                "requested_end": self.window.end_date.isoformat(),
                                "last_session": calendar[-1].isoformat()})
        if self.settings.execution.end_policy == "force-close":
            self._force_close(calendar[-1])
        return self._artifacts(calendar)

    def _advance_session(self, session: date, start: date) -> None:
        tradable = session >= start
        for series in self.series.values():
            indices = series.by_date.get(session)
            if not indices:
                continue
            # A duplicated session for one symbol uses the last print only.
            for index in indices:
                candle = series.candles[index]
                self._push(series, candle)
                self._manage(series, candle)
                if tradable:
                    self._drain(series, candle, index)

    def _push(self, series: _SymbolSeries, candle: OHLCVCandle) -> None:
        if series.store is None:
            series.store = OHLCVStore(
                window=max(DEFAULT_WINDOW, len(series.candles)),
                session_tz=self.session_tz,
            )
        series.store.apply_candle(series.symbol, self.settings.timeframe, candle)
        series.pushed += 1
        series.last_close = candle.close
        series.last_date = _session_date(candle, self.session_tz)

    def _manage(self, series: _SymbolSeries, candle: OHLCVCandle) -> None:
        if not series.open_positions:
            return
        bar_index = series.pushed
        still_open: list[BacktestTrade] = []
        for position in series.open_positions:
            _update_neckline_state(position, candle, bar_index)
            exit_price, reason = _check_exit(candle, position, bar_index)
            if exit_price is not None:
                _close_trade(position, exit_price, reason, candle, self.txn_cost)
                series.closed.append(position)
                self._closed_order.append(position)
            else:
                _update_prev_hl(position, candle)
                still_open.append(position)
        series.open_positions = still_open

    def _drain(self, series: _SymbolSeries, candle: OHLCVCandle, index: int) -> None:
        from patterns import _dedup

        if series.store is None:
            return
        # Causal semantics: the store holds only bars up to this session, so
        # detectors use len(df)-1 as the true current bar (paper's behavior).
        # The dedup registry is per (symbol, session): the channel memo is keyed
        # by generation and must be invalidated whenever the series grows.
        with _ANALYZE_LOCK:
            _dedup.reset()
            _dedup._used = set(series.used)
            snapshot = _make_snapshot(series.symbol, self.settings.timeframe, candle)
            for pattern in self.patterns:
                if self.settings.timeframe not in pattern.timeframes:
                    continue
                max_open = getattr(pattern, "MAX_OPEN_PER_SYMBOL", None)
                if max_open is not None and sum(
                    1 for p in series.open_positions if p.pattern == pattern.name
                ) >= max_open:
                    continue
                for _ in range(8):
                    signal = pattern.analyze(snapshot, series.store)
                    if signal is None:
                        break
                    key = signal.setup_key or ()
                    if _dedup.any_used(key):
                        break
                    self.total_signals += 1
                    if getattr(signal, "blocked_reason", None):
                        _dedup.mark(*key)
                        series.used.update(key)
                        self.events.append({"symbol": series.symbol, "status": "blocked",
                                            "session": candle.timestamp.isoformat(),
                                            "reason": signal.blocked_reason})
                        continue
                    if getattr(signal, "filtered_reason", None):
                        _dedup.mark(*key)
                        series.used.update(key)
                        self.events.append({"symbol": series.symbol, "status": "filtered",
                                            "session": candle.timestamp.isoformat(),
                                            "reason": signal.filtered_reason})
                        continue
                    if not self._gates_allow(signal, series.store):
                        _dedup.mark(*key)
                        series.used.update(key)
                        continue
                    _apply_notional_sizing(signal, self.notional,
                                           fractional=is_fractional_qty(signal.pattern))
                    if signal.qty <= 0 or (
                        signal.qty < 1 and not is_fractional_qty(signal.pattern)
                    ):
                        _dedup.mark(*key)
                        series.used.update(key)
                        continue
                    signal.signal_bar_idx = index
                    signal.signal_bar_timestamp = candle.timestamp
                    if self.lot_round:
                        signal.price = signal.price or candle.close
                        if not apply_lot_rounding(signal):
                            _dedup.mark(*key)
                            series.used.update(key)
                            continue
                    series.open_positions.append(_open_trade(signal, candle, series.pushed))
                    _dedup.mark(*key)
                    series.used.update(key)
            series.used = set(_dedup._used)

    def _gates_allow(self, signal, store) -> bool:
        if not self.settings.execution.volume_gate:
            return True
        from analysis.price_volume import volume_confirm_gate

        try:
            return volume_confirm_gate(signal, store).passed
        except Exception:  # noqa: BLE001 - a gate failure must not fabricate a trade
            return False

    def _mark_equity(self, session: date) -> None:
        realized = sum(t.pnl_usd for t in self._closed_order)
        unrealized = 0.0
        for series in self.series.values():
            if not series.open_positions or series.last_close is None:
                continue
            for position in series.open_positions:
                if position.action == "BUY":
                    unrealized += (series.last_close - position.entry_price) * position.qty
                else:
                    unrealized += (position.entry_price - series.last_close) * position.qty
        equity = self.settings.execution.initial_capital + realized + unrealized
        self.equity_curve.append({"date": session.isoformat(), "realized_pnl": round(realized, 6),
                                  "unrealized_pnl": round(unrealized, 6),
                                  "equity": round(equity, 6)})

    def _force_close(self, session: date) -> None:
        for series in self.series.values():
            if not series.open_positions:
                continue
            candle = series.candles[-1]
            for position in series.open_positions:
                _close_trade(position, candle.close, "force_close", candle, self.txn_cost)
                series.closed.append(position)
                self._closed_order.append(position)
            series.open_positions = []
            self.events.append({"symbol": series.symbol, "status": "force-closed",
                                "session": session.isoformat()})

    # ── artifacts ────────────────────────────────────────────────────────
    def _artifacts(self, calendar: list[date]) -> dict:
        closed = sorted(self._closed_order, key=lambda t: t.exit_date)
        initial = self.settings.execution.initial_capital
        open_positions = []
        unrealized = 0.0
        for series in self.series.values():
            for position in series.open_positions:
                usd = 0.0
                if series.last_close is not None:
                    move = (series.last_close - position.entry_price
                            if position.action == "BUY"
                            else position.entry_price - series.last_close)
                    usd = move * position.qty
                unrealized += usd
                open_positions.append({
                    "symbol": series.symbol, "pattern": position.pattern,
                    "version_id": position.pattern_version_id, "action": position.action,
                    "entry_date": position.entry_date.isoformat(),
                    "entry_price": position.entry_price, "qty": position.qty,
                    "stop_loss": position.stop_loss, "take_profit": position.take_profit,
                    "unrealized_pct": self._unrealized_pct(series, position),
                    "unrealized_usd": round(usd, 6),
                })
        peak = 0.0
        drawdown = 0.0
        for point in self.equity_curve:
            gain = point["equity"] - initial
            peak = max(peak, gain)
            drawdown = max(drawdown, peak - gain)
        fees = 0.0
        for trade in closed:
            fees += (trade.entry_price + trade.exit_price) * self.txn_cost * trade.qty
        for series in self.series.values():
            for position in series.open_positions:
                fees += position.entry_price * self.txn_cost * position.qty
        realized = sum(t.pnl_usd for t in closed)
        trades = {
            "meta": {"mode": "historical-stream", "sessions": len(calendar),
                     "total_signals": self.total_signals, "trades": len(closed),
                     "open_positions": len(open_positions),
                     "first_session": calendar[0].isoformat(),
                     "last_session": calendar[-1].isoformat()},
            "trades": [self._trade_row(t) for t in closed],
        }
        return {
            "trades": trades,
            "signals": {"total_signals": self.total_signals, "events": self.events},
            "equity": self.equity_curve,
            "open_positions": open_positions,
            "logs": {"sessions": len(calendar), "events": len(self.events)},
            "metrics": {
                "trade_count": len(closed),
                "wins": sum(1 for t in closed if t.pnl_pct > 0),
                "realized_pnl": round(realized, 6),
                "unrealized_pnl": round(unrealized, 6),
                "open_count": len(open_positions),
                "fees": round(fees, 6),
                "drawdown_pct": (drawdown / initial * 100.0) if initial > 0 else 0.0,
            },
        }

    def _unrealized_pct(self, series: _SymbolSeries, position: BacktestTrade) -> float | None:
        if series.last_close is None or position.entry_price <= 0:
            return None
        move = (series.last_close - position.entry_price
                if position.action == "BUY" else position.entry_price - series.last_close)
        return round(move / position.entry_price * 100, 6)

    def _trade_row(self, trade: BacktestTrade) -> dict:
        from core.pattern_provenance import payload as provenance

        return {
            "sym": trade.symbol, "pattern": trade.pattern, "timeframe": trade.timeframe,
            "action": trade.action, "entryDate": trade.entry_date.isoformat(),
            "entryPrice": round(trade.entry_price, 4), "shares": round(trade.qty, 4),
            "exitDate": trade.exit_date.isoformat(), "exitPrice": round(trade.exit_price, 4),
            "exitReason": trade.exit_reason, "pnlPct": round(trade.pnl_pct, 4),
            "pnlUSD": round(trade.pnl_usd, 2), "barsHeld": (
                (trade.exit_bar_idx - trade.entry_bar_idx)
                if trade.exit_bar_idx is not None else None),
            **provenance(trade),
        }
