"""Isolated historical-stream replay shared with paper trading's execution path.

The replay advances one simulated session at a time and drives the *same*
trade-management functions the paper account uses — ``_open_trade`` /
``_check_exit`` / ``_close_trade`` / ``_update_neckline_state`` from
``core.backtester``, applied in the same order as ``PaperAccount.on_bar``
(fill pending entries at the open, manage exits, then queue close decisions).
Selection, opening-price validation, sizing, exits and P&L use shared helpers.

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
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone

from core.backtester import (
    BacktestTrade,
    _check_exit,
    _close_trade,
    _make_snapshot,
    _min_required_bars,
    _open_trade,
    _update_neckline_state,
    _update_prev_hl,
)
from core.market import get_market
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
        o, h, l, c, v = map(float, (open_, high, low, close, volume))
        if (not all(math.isfinite(x) and x > 0 for x in (o, h, l, c))
                or not math.isfinite(v) or v < 0 or h < max(o, l, c) or l > min(o, h, c)):
            raise EditError("Frozen history contains an invalid OHLCV bar")
        out.append(OHLCVCandle(open=o, high=h, low=l, close=c, volume=v, timestamp=ts))
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
    pending_entries: list = field(default_factory=list)
    consumed_anchors: set[tuple] = field(default_factory=set)
    open_positions: list[BacktestTrade] = field(default_factory=list)
    closed: list[BacktestTrade] = field(default_factory=list)
    last_close: float | None = None
    last_candle: OHLCVCandle | None = None
    last_date: date | None = None


class StreamReplay:
    """One isolated, causal, session-at-a-time historical replay."""

    def __init__(self, *, dataset: dict[str, list[list]], patterns, settings,
                 session_tz: str, cancel=None, progress=None,
                 laya_mode: str = "off", laya_assess=None):
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
        if any(p.name == "vision_confirmation_enabled" and p.value for p in settings.execution.parameters):
            raise EditError("Historical replay does not support vision confirmation")
        self.parameters = {p.name: p.value for p in settings.execution.parameters}
        if settings.execution.volume_gate:
            rvol = self.parameters.get("volume_gate_rvol_min")
            obv = self.parameters.get("volume_gate_obv_bars")
            if (not isinstance(rvol, (float, int)) or isinstance(rvol, bool)
                    or not math.isfinite(rvol) or rvol <= 0
                    or not isinstance(obv, int) or isinstance(obv, bool) or obv < 1):
                raise EditError("Re-save the preset with finite positive RVOL and integer OBV gate thresholds")
        self.notional = settings.execution.position_notional
        self.txn_cost = settings.execution.txn_cost_pct
        self.slippage = settings.execution.slippage_pct
        self.long_only = get_market(settings.market).long_only
        self.patterns = list(patterns)
        self.events: list[dict] = []
        self.series = self._build_series(dataset)
        if not self.series:
            raise EditError("No frozen daily history available for the selected symbols")
        self._collect_pool = []
        self.total_signals = 0
        self._closed_order: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []
        # Laya historical filtering (docs/laya.md P4). Off by default.
        # `laya_assess` maps an owned candidate dict -> {"outcome": allow/veto/abstain,
        # "reasons": [...], "assessment_id": ...}. A missing decision has a defined
        # failure policy (fail-closed in filter mode), never a silent bypass.
        laya_setting = getattr(settings, "laya", None)
        if laya_setting is not None and getattr(laya_setting, "mode", "off") != "off":
            if laya_mode == "off":
                laya_mode = str(laya_setting.mode)
        if laya_mode not in ("off", "shadow", "filter"):
            raise EditError(f"Unsupported Laya mode: {laya_mode}")
        if laya_mode != "off" and getattr(settings.execution, "collect_first", 0):
            raise EditError("Laya experiments do not support collect-first yet")
        self.laya_mode = laya_mode
        self.laya_assess = laya_assess
        self.laya_stats: dict[str, int] = {"assessed": 0, "allowed": 0, "vetoed": 0,
                                           "abstained": 0, "missing": 0}

    # ── setup ────────────────────────────────────────────────────────────
    def _build_series(self, dataset: dict[str, list[list]]) -> dict[str, _SymbolSeries]:
        series: dict[str, _SymbolSeries] = {}
        for symbol, rows in sorted(dataset.items()):
            candles = [c for c in rows_to_candles(rows) if c.timestamp is not None]
            if not candles:
                continue
            deduped: list[OHLCVCandle] = []
            for candle in candles:
                if deduped and candle.timestamp < deduped[-1].timestamp:
                    self.events.append({"symbol": symbol, "status": "unordered-bar",
                                        "session": candle.timestamp.isoformat()})
                    continue
                if deduped and _session_date(candle, self.session_tz) == _session_date(deduped[-1], self.session_tz):
                    deduped[-1] = candle
                    self.events.append({"symbol": symbol, "status": "duplicate-bar",
                                        "session": candle.timestamp.isoformat()})
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
            self._mark_equity(calendar[-1])
        for series in self.series.values():
            for signal in series.pending_entries:
                self.events.append({"symbol": series.symbol, "pattern": signal.pattern,
                                    "status": "unfilled", "reason": "end-of-data",
                                    "session": signal.signal_bar_timestamp.isoformat()})
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
                self._fill_pending(series, candle)
                self._manage(series, candle)
                if tradable:
                    self._drain(series, candle, index)
        if self.settings.execution.collect_first:
            from core.execution import rank_candidates
            selected, rejected = rank_candidates(self._collect_pool, self.settings.execution.collect_first)
            self._collect_pool = []
            for signal, series, candle in rejected:
                self.events.append({"symbol": signal.symbol, "pattern": signal.pattern,
                                    "session": candle.timestamp.isoformat(), "status": "rejected",
                                    "reason": "Collect-first: below selected top-N"})
            for signal, series, candle in selected:
                if self._gates_allow(signal, series.store):
                    self._queue(series, signal)

    def _fill_pending(self, series, candle):
        from core.execution import opening_candidate, opening_observation
        for signal in list(series.pending_entries):
            try:
                fill = opening_candidate(signal, candle, self.notional, self.settings.market, self.slippage)
                if fill is None:
                    continue
                # A historical Laya decision applies to its decision bar only.
                # Filter assessments must be refreshed at execution, fail closed.
                if (signal.laya_assessment or {}).get("mode") == "filter":
                    if self.laya_assess is None:
                        raise ValueError("Missing Laya execution assessment")
                    observation = opening_observation(candle, self.settings.market)
                    verdict = self.laya_assess({"symbol": series.symbol, "signal": fill,
                                               "session": observation.timestamp.isoformat(),
                                               "bar_ts": observation.timestamp.isoformat(),
                                               "bar_close": observation.close,
                                               "phase": "execution", "price": candle.open,
                                               "timeframe": signal.timeframe, "market": self.settings.market,
                                               "pattern": signal.pattern, "pattern_version_id": signal.pattern_version_id,
                                               "action": signal.action, "stop": signal.stop_loss, "target": signal.take_profit,
                                               "confidence": signal.confidence, "bar_index": series.pushed - 1})
                    if not isinstance(verdict, dict) or verdict.get("outcome") != "allow":
                        raise ValueError("Laya execution assessment did not allow this entry")
                    fill.laya_assessment = {**signal.laya_assessment, **verdict}
                price = fill.price
                cap = (signal.requested_rules or {}).get("max_open_per_symbol")
                if cap is None:
                    cap = next((getattr(p, "MAX_OPEN_PER_SYMBOL", None) for p in self.patterns
                                if p.name == signal.pattern), None)
                if cap is not None and sum(p.pattern == signal.pattern for p in series.open_positions) >= cap:
                    raise ValueError("MAX_OPEN_PER_SYMBOL reached at execution")
                point = replace(candle, open=price, high=price, low=price, close=price)
                position = _open_trade(fill, point, series.pushed - 1, execution_model="causal-v1")
                position.entry_fee_paid = price * position.qty * self.txn_cost
                series.open_positions.append(position)
                status, reason = "filled", "next-observed-open"
            except Exception as exc:
                # Assessor/runtime failures reject this entry, never interrupt exits.
                status, reason = "rejected", str(exc)
            series.pending_entries.remove(signal)
            self.events.append({"symbol": series.symbol, "pattern": signal.pattern,
                                "session": candle.timestamp.isoformat(), "status": status, "reason": reason})

    def _history_window(self):
        value = next((p.value for p in self.settings.execution.parameters if p.name == "history_window"), DEFAULT_WINDOW)
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise EditError("history_window must be an integer of at least two bars")
        return value

    def _push(self, series: _SymbolSeries, candle: OHLCVCandle) -> None:
        if series.store is None:
            series.store = OHLCVStore(
                window=self._history_window(),
                session_tz=self.session_tz,
            )
        series.store.apply_candle(series.symbol, self.settings.timeframe, candle)
        series.pushed += 1
        series.last_close = candle.close
        series.last_candle = candle
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
                exit_price *= 1 + self.slippage if position.action == "SELL" else 1 - self.slippage
                _close_trade(position, exit_price, reason, candle, self.txn_cost)
                series.closed.append(position)
                self._closed_order.append(position)
            else:
                _update_prev_hl(position, candle)
                still_open.append(position)
        series.open_positions = still_open

    def _queue(self, series, signal):
        from core.pattern_provenance import rules
        signal.requested_rules = signal.requested_rules or rules(signal, entry_mode="decision-close")
        key = (signal.pattern, tuple(signal.setup_key or ()))
        if key in series.consumed_anchors:
            self.events.append({"symbol": series.symbol, "pattern": signal.pattern,
                                "session": signal.signal_bar_timestamp.isoformat(),
                                "status": "rejected", "reason": "Duplicate consumed setup"})
            return
        series.consumed_anchors.add(key)
        series.pending_entries.append(signal)
        self.events.append({"symbol": series.symbol, "pattern": signal.pattern,
                            "session": signal.signal_bar_timestamp.isoformat(),
                            "status": "queued", "reason": "next-observed-open"})

    def _drain(self, series: _SymbolSeries, candle: OHLCVCandle, index: int) -> None:
        from patterns import _dedup
        from core.execution import stable_anchors
        from config import PATTERN_SCAN_HISTORY_BARS

        if series.store is None:
            return
        pending = []
        with _ANALYZE_LOCK:
            _dedup.reset()
            try:
                snapshot = _make_snapshot(series.symbol, self.settings.timeframe, candle)
                for pattern in self.patterns:
                    if self.settings.timeframe not in pattern.timeframes:
                        continue
                    if series.store.available(series.symbol, self.settings.timeframe) < max(
                            PATTERN_SCAN_HISTORY_BARS, int(getattr(pattern, "MIN_BARS", 2) or 2)):
                        continue
                    # Like the scanner: one candidate per pattern and completed bar.
                    signal = pattern.analyze(snapshot, series.store)
                    if signal is None:
                        continue
                    self.total_signals += 1
                    self.events.append({"status": "detected", "symbol": series.symbol,
                                        "session": candle.timestamp.isoformat(), "pattern": signal.pattern,
                                        "pattern_version_id": signal.pattern_version_id,
                                        "action": signal.action, "entry": signal.price or candle.close,
                                        "stop": signal.stop_loss, "target": signal.take_profit,
                                        "chart_annotations": signal.chart_annotations})
                    reason = signal.blocked_reason or signal.filtered_reason
                    if reason or signal.action not in ("BUY", "SELL") or (self.long_only and signal.action == "SELL"):
                        self.events.append({"symbol": series.symbol, "status": "rejected",
                                            "session": candle.timestamp.isoformat(),
                                            "reason": reason or "Unsupported entry action"})
                        continue
                    signal.signal_bar_idx = series.pushed
                    signal.signal_bar_timestamp = candle.timestamp
                    stable_anchors(signal, series.store)
                    if self.settings.execution.collect_first:
                        self._collect_pool.append((signal, series, candle))
                    elif self._gates_allow(signal, series.store):
                        if self.laya_mode == "off":
                            self._queue(series, signal)
                        else:
                            pending.append({"signal": signal, "pushed": series.pushed,
                                            "session": candle.timestamp.isoformat()})
            finally:
                _dedup.reset()
        # Inference never holds the detector lock.
        for item in pending:
            self._open_pending_laya(series, candle, item)

    def _open_pending_laya(self, series: _SymbolSeries, candle: OHLCVCandle, item: dict) -> None:
        """Evaluate one owned candidate outside the analysis lock."""
        from copy import deepcopy

        signal = item["signal"]
        outcome = "allow"
        reasons: list[str] = ["laya off-path (no assessor)"]
        assessment_id: str | None = None
        if self.laya_assess is not None:
            try:
                verdict = self.laya_assess({
                    "symbol": series.symbol,
                    "timeframe": self.settings.timeframe,
                    "market": self.settings.market,
                    "pattern": signal.pattern,
                    "pattern_version_id": signal.pattern_version_id,
                    "action": signal.action,
                    "price": signal.price,
                    "stop": signal.stop_loss,
                    "target": signal.take_profit,
                    "confidence": signal.confidence,
                    "session": item["session"],
                    "bar_index": item["pushed"],
                    "signal": deepcopy(signal),
                })
            except Exception as exc:  # noqa: BLE001 - failures never bypass filtering
                verdict = {"outcome": "abstain", "reasons": [f"assessor error: {exc}"]}
            if isinstance(verdict, dict):
                outcome = str(verdict.get("outcome") or "abstain")
                reasons = [str(r) for r in (verdict.get("reasons") or ["no reason"])]
                assessment_id = verdict.get("assessment_id")
            else:
                outcome, reasons = "abstain", ["assessor returned no decision"]
        else:
            # No precomputed decision: defined failure policy, never silent bypass.
            if self.laya_mode == "filter":
                outcome, reasons = "abstain", ["missing precomputed Laya decision"]
            else:
                outcome, reasons = "allow", ["shadow mode without assessor: baseline fill"]
        self.laya_stats["assessed"] += 1
        event = {"symbol": series.symbol, "status": f"laya-{outcome}",
                 "session": item["session"], "pattern": signal.pattern,
                 "action": signal.action, "mode": self.laya_mode,
                 "reasons": reasons, "assessment_id": assessment_id}
        self.events.append(event)
        # Stamp the verdict on the signal so _open_trade carries it onto the
        # trade for Paper/export attribution.
        laya_setting = getattr(self.settings, "laya", None)
        signal.laya_assessment = {
            "assessment_id": assessment_id,
            "policy_id": (getattr(laya_setting, "policy_id", None)
                          or "laya-event-v1-default"),
            "mode": self.laya_mode,
            "outcome": outcome,
        }
        if self.laya_mode == "shadow":
            # Shadow records the decision with no effect on fills.
            if outcome == "allow":
                self.laya_stats["allowed"] += 1
            elif outcome == "veto":
                self.laya_stats["vetoed"] += 1
            else:
                self.laya_stats["abstained"] += 1
            self._queue(series, signal)
            return
        # Filter mode: allow opens, veto/abstain skips the new entry.
        if outcome == "allow":
            self.laya_stats["allowed"] += 1
            self._queue(series, signal)
        elif outcome == "veto":
            self.laya_stats["vetoed"] += 1
        else:
            self.laya_stats["abstained"] += 1

    def _gates_allow(self, signal, store) -> bool:
        if not self.settings.execution.volume_gate:
            return True
        from analysis.price_volume import volume_confirm_gate

        try:
            gate = volume_confirm_gate(signal, store,
                                       rvol_min=self.parameters["volume_gate_rvol_min"],
                                       obv_bars=self.parameters["volume_gate_obv_bars"])
            if gate.passed:
                return True
            reason = gate.reason
        except Exception as exc:
            reason = f"Volume gate failed: {exc}"
        self.events.append({"symbol": signal.symbol, "pattern": signal.pattern,
                            "session": signal.signal_bar_timestamp.isoformat(),
                            "status": "rejected", "reason": reason})
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
        unrealized -= sum(p.entry_fee_paid for series in self.series.values() for p in series.open_positions)
        equity = self.settings.execution.initial_capital + realized + unrealized
        point = {"date": session.isoformat(), "realized_pnl": round(realized, 6),
                 "unrealized_pnl": round(unrealized, 6), "equity": round(equity, 6)}
        if self.equity_curve and self.equity_curve[-1]["date"] == point["date"]:
            self.equity_curve[-1] = point
        else:
            self.equity_curve.append(point)

    def _force_close(self, session: date) -> None:
        for series in self.series.values():
            if not series.open_positions:
                continue
            candle = series.last_candle
            if candle is None:
                continue
            for position in series.open_positions:
                position.exit_bar_idx = series.pushed
                exit_price = candle.close * (
                    1 + self.slippage if position.action == "SELL" else 1 - self.slippage
                )
                _close_trade(position, exit_price, "force_close", candle, self.txn_cost)
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
                usd -= position.entry_fee_paid
                unrealized += usd
                open_positions.append({
                    "symbol": series.symbol, "pattern": position.pattern,
                    "version_id": position.pattern_version_id, "action": position.action,
                    "entry_date": position.entry_date.isoformat(),
                    "entry_price": position.entry_price, "qty": position.qty,
                    "current_price": series.last_close,
                    "position_value": (series.last_close or 0.0) * position.qty,
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
            "meta": {"mode": "historical-stream", "evaluation": "causal-research-replay",
                     "account_model": "independent-fixed-notional", "execution_model": "causal-v1",
                     "entry_policy": "next-observed-open", "history_window": self._history_window(), "sessions": len(calendar),
                     "total_signals": self.total_signals, "trades": len(closed),
                     "open_positions": len(open_positions),
                     "first_session": calendar[0].isoformat(),
                     "last_session": calendar[-1].isoformat(),
                     "laya_mode": self.laya_mode,
                     "laya_stats": dict(self.laya_stats)},
            "trades": [self._trade_row(t) for t in closed],
        }
        laya_setting = getattr(self.settings, "laya", None)
        laya_manifest = None
        if laya_setting is not None:
            laya_manifest = laya_setting.model_dump(mode="json")
        return {
            "trades": trades,
            "signals": {"total_signals": self.total_signals, "events": self.events},
            "equity": self.equity_curve,
            "open_positions": open_positions,
            "logs": {"sessions": len(calendar), "events": len(self.events),
                     "exposure": {"gross_notional": sum(p["position_value"] for p in open_positions),
                                  "net_notional": sum(p["position_value"] * (1 if p["action"] == "BUY" else -1)
                                                      for p in open_positions)}},
            "laya": {"mode": self.laya_mode, "stats": dict(self.laya_stats),
                     "manifest": laya_manifest},
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
