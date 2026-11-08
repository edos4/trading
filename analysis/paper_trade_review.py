"""Verify and review an Export Trades file using only its saved OHLC evidence.

Run ``python -m analysis.paper_trade_review pt.json --output /tmp/review.json``.
No tape downloads, detector rescans, model inference or account writes occur.
Counterfactual targets must resolve within the originally observed bars.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median

from core.backtester import (
    _check_exit, _close_trade, _open_trade,
    _update_neckline_state, _update_prev_hl, causal_exit_runtime_compatible, trade_risk_dollars,
)
from core.pattern_provenance import RULE_FIELDS, engine_hash
from core.position_protection import ensure_paper_protection
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def _equal(actual: float, expected: float, field: str, tolerance: float = .00006) -> None:
    if not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0):
        raise ValueError(f"Saved {field} does not reproduce: {actual} != {expected}")


def summarize(rows: list[dict]) -> dict:
    """Net results in one book's currency; zero P&L is neither a win nor loss."""
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    n, w = len(rows), len(wins)
    avg_win, avg_loss = mean(wins) if wins else 0, mean(losses) if losses else 0
    interval = None
    if n:
        # Wilson score interval. Trades can be dependent; this is descriptive,
        # not a claim of independent trials or a calibrated win probability.
        z, p = 1.959963984540054, w / n
        center = (p + z*z / (2*n)) / (1 + z*z/n)
        half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
        interval = [100*(center-half), 100*(center+half)]
    return {
        "count": n, "wins": w, "losses": len(losses),
        "net_pnl": sum(r["pnl"] for r in rows),
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "win_rate_pct": w / n * 100 if n else None,
        "descriptive_win_rate_95pct_interval": interval,
        "average_win": avg_win, "average_loss": avg_loss,
        "expectancy_per_trade": mean(r["pnl"] for r in rows) if rows else None,
        "average_net_r": mean(r["net_r"] for r in rows if r["net_r"] is not None)
        if any(r["net_r"] is not None for r in rows) else None,
        "breakeven_win_rate_pct": -avg_loss / (avg_win-avg_loss) * 100
        if avg_win > 0 and avg_loss < 0 else None,
        "exit_reasons": dict(Counter(r["exit_reason"] for r in rows)),
    }


def _orders(position) -> dict:
    from core.paper_trader import exit_state
    return exit_state(position)


def _verify_orders(actual: dict, saved: dict) -> None:
    if actual.keys() != saved.keys():
        raise ValueError("Saved prior orders have a different schema")
    for key, value in actual.items():
        expected = saved[key]
        if isinstance(value, (float, int)) and not isinstance(value, bool) and isinstance(expected, (float, int)):
            _equal(value, expected, f"prior orders: {key}", .00000001)
        elif value != expected:
            raise ValueError(f"Saved prior orders do not reproduce: {key}")


def replay_trade(trade: dict, *, target_max_r: float | None = None,
                 target_price: float | None = None,
                 trailing_stop_basis: str | None = None,
                 paper_loss_protection: bool = False,
                 allow_inconsistent_ohlc: bool = False) -> dict:
    """Reproduce saved fills, inferring each trade's actual fee/slippage rates."""
    rules = trade.get("resolved_rules") or {}
    if not causal_exit_runtime_compatible(rules.get("engine_sha256")):
        raise ValueError("Saved exit runtime differs; restore the compatible engine")
    if trade.get("execution_model") != "causal-v1":
        raise ValueError("This review requires causal-v1 execution")
    marks = trade.get("daily_marks") or []
    if not marks or any(not m.get("exit_check") for m in marks):
        raise ValueError("Saved OHLC exit evidence is required; never infer it from fills")
    if any(m["exit_check"].get("observation_kind") == "live_quote" for m in marks):
        raise ValueError("Live quote exits require tick evidence; a completed-bar replay cannot verify them")
    bars, anomalies = [], []
    for mark in marks:
        stamp = datetime.fromisoformat(mark["sim_bar"])
        raw = mark["exit_check"]["ohlc"]
        if stamp.tzinfo is None or any(not math.isfinite(raw[k]) or raw[k] <= 0
                                     for k in ("open", "high", "low", "close")):
            raise ValueError("Invalid saved OHLC evidence")
        if raw["high"] < max(raw.values()) or raw["low"] > min(raw.values()):
            if not allow_inconsistent_ohlc:
                raise ValueError("Inconsistent saved OHLC range")
            anomalies.append({"sim_bar": mark["sim_bar"], "ohlc": dict(raw)})
        bars.append(OHLCVCandle(timestamp=stamp, volume=0, **raw))
    if bars[0].timestamp.isoformat() != trade["sim_opened"] or any(
            a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError("Entry bar, ordering or uniqueness of saved marks is invalid")
    if datetime.fromisoformat(trade["decision_date"]) >= bars[0].timestamp:
        raise ValueError("Entry must follow its completed-bar decision")
    entry, qty = rules["price"], rules["qty"]
    if not all(math.isfinite(v) and v > 0 for v in (entry, qty)):
        raise ValueError("Invalid saved entry or quantity")
    _equal(entry, trade["entry"], "entry")
    _equal(qty, trade["qty"], "quantity")
    short = trade["action"] == "SELL"
    sign = -1 if short else 1
    slip = sign * (entry / bars[0].open - 1)
    fee = trade["entry_fee_paid"] / (entry * qty)
    if not math.isfinite(fee) or fee < 0 or not -.000000000001 <= slip < 1:
        raise ValueError("Invalid inferred fee or slippage rate")
    slip = max(0.0, slip)
    signal = TradeSignal(
        symbol=trade["symbol"], timeframe=trade["timeframe"], pattern=trade["pattern"],
        action=trade["action"], confidence=1, price=entry, qty=qty,
        provenance=trade["provenance"],
        **{k: rules.get(k) for k in RULE_FIELDS if k != "execution_model"},
    )
    point = replace(bars[0], open=entry, high=entry, low=entry, close=entry)
    position = _open_trade(signal, point, 0, execution_model="causal-v1")
    saved_policy = trade.get("paper_protection")
    if saved_policy and saved_policy.get("policy") != "net-loss-trail-v1":
        raise ValueError("Unknown saved paper protection policy")
    if paper_loss_protection:
        ensure_paper_protection(position, fee, slip, bars[0].timestamp, at_entry=True)
    elif saved_policy and saved_policy["initial_stop"] is not None:
        ensure_paper_protection(position, saved_policy["txn_cost_pct"], saved_policy["slippage_pct"],
                                datetime.fromisoformat(saved_policy["adopted_at"]), at_entry=True)
    risk = trade_risk_dollars(position)
    counterfactual = (target_max_r is not None or target_price is not None
                      or trailing_stop_basis is not None or paper_loss_protection)
    if target_price is not None:
        if not math.isfinite(target_price) or target_price <= 0 or sign * (target_price - entry) <= 0:
            raise ValueError("Counterfactual target must remain beyond the executed entry")
        position.take_profit = target_price
    if trailing_stop_basis is not None:
        if trailing_stop_basis not in ("reference", "entry"):
            raise ValueError("Unknown trailing stop basis")
        position.trailing_stop_basis = trailing_stop_basis
    if target_max_r is not None:
        if not math.isfinite(target_max_r) or target_max_r <= 0 or not risk:
            raise ValueError("Target cap requires positive finite R and initial risk")
        target = entry + sign * target_max_r * risk / qty
        if position.take_profit is not None and sign*(position.take_profit-target) > 0:
            position.take_profit = round(target, 4)
    armed, paper_armed, ambiguous, gap_fill, best_close_r = False, False, [], False, 0.0
    for index, (bar, mark) in enumerate(zip(bars, marks), 1):
        if saved_policy and not paper_loss_protection and bar.timestamp >= datetime.fromisoformat(saved_policy["adopted_at"]):
            ensure_paper_protection(position, saved_policy["txn_cost_pct"], saved_policy["slippage_pct"],
                                    datetime.fromisoformat(saved_policy["adopted_at"]),
                                    at_entry=saved_policy["initial_stop"] is not None)
        # External next-open decisions cannot be regenerated by the OHLC exit
        # ladder. Preserve the saved order, including in bounded comparisons.
        pending = mark["exit_check"]["orders_before_bar"].get("pending_exit_reason")
        if pending == "kronos_exit" and not position.pending_exit_reason:
            position.pending_exit_reason = pending
        orders = _orders(position)
        if not counterfactual:
            _verify_orders(orders, mark["exit_check"]["orders_before_bar"])
        stops = [orders[k] for k, flag in (("hard_stop", "hard_stop_on_close"),
                                         ("trailing_stop", "trailing_stop_on_close"))
                 if orders[k] is not None and not orders[flag]]
        if orders.get("paper_protective_stop") is not None:
            stops.append(orders["paper_protective_stop"])
        stop = (min(stops) if short else max(stops)) if stops else None
        target = orders["target"] if not orders["target_on_close"] else None
        gap_stop = stop is not None and sign*(bar.open-stop) <= 0
        gap_target = target is not None and sign*(bar.open-target) >= 0
        if (stop is not None and target is not None and not (gap_stop or gap_target)
                and not orders["pending_exit_reason"]
                and (bar.high >= stop and bar.low <= target if short else
                     bar.low <= stop and bar.high >= target)):
            ambiguous.append(bar.timestamp.isoformat())
        _update_neckline_state(position, bar, index)
        price, reason = _check_exit(bar, position, index)
        if not counterfactual:
            check = mark["exit_check"]
            if (reason or None) != check["reason"] or (price is None) != (check["raw_fill"] is None):
                raise ValueError(f"Saved exit decision does not reproduce for {trade['symbol']}")
            if price is not None:
                _equal(price, check["raw_fill"], "raw exit fill", .00000001)
        armed = armed or position._trailing_activated
        paper_armed = paper_armed or (position.paper_protection or {}).get("peak_net_pct", 0) > 0
        if price is not None:
            trigger = target if reason == "take_profit" else stop
            gap_fill = (trigger is not None and not orders["pending_exit_reason"]
                        and not math.isclose(price, trigger, abs_tol=.000001))
            _close_trade(position, price * (1 - sign*slip), reason, bar, fee)
            break
        if risk:
            best_close_r = max(best_close_r, sign*(bar.close-entry)*qty/risk)
        _update_prev_hl(position, bar)
        position.position_marks.append(deepcopy(mark))
    closed = bool(position.exit_reason)
    marked = sign*(bars[-1].close-entry)*qty - trade["entry_fee_paid"]
    liquidation = bars[-1].close * (1-sign*slip)
    net = position.pnl_usd if closed else (
        sign*(liquidation-entry) - (entry+liquidation)*fee) * qty
    if not counterfactual:
        if saved_policy:
            _verify_orders(position.paper_protection, saved_policy)
        if bool(trade.get("exit_reason")) != closed:
            raise ValueError("Saved open/closed state does not reproduce")
        if closed:
            if (bar.timestamp.isoformat(), reason, index) != (
                    trade["sim_closed"], trade["exit_reason"], trade["hold_bars"]):
                raise ValueError("Saved exit date, reason or bar count does not reproduce")
            _equal(position.exit_price, trade["exit"], "exit fill")
            _equal(net, trade["pnl"], "net P&L")
            _equal((entry+position.exit_price)*qty*fee,
                   marks[-1]["exit_check"]["fees"], "fees")
        else:
            _equal(marked, trade["mtm"], "open marked P&L")
    elif trade.get("exit_reason") and not closed and trailing_stop_basis is None and not paper_loss_protection:
        raise ValueError("Counterfactual exceeds saved coverage; obtain later bars")
    return {
        "trade_id": trade["trade_id"], "symbol": trade["symbol"],
        "action": trade["action"], "pattern": trade["pattern"],
        "entry_date": trade["sim_opened"], "last_observed": bars[-1].timestamp.isoformat(),
        "exit_date": bar.timestamp.isoformat() if closed else None,
        "exit_reason": position.exit_reason or "open", "pnl": net,
        "net_return_pct": net / (entry * qty) * 100,
        "net_r": net/risk if risk else None, "initial_risk": risk,
        "risk_pct": risk/(entry*qty)*100 if risk else None,
        "target_r": sign*(position.take_profit-entry)*qty/risk
        if position.take_profit is not None and risk else None,
        "activation_r": position.trailing_activation_pct*entry*qty/risk
        if position.trailing_activation_pct is not None and risk else None,
        "entry": entry, "qty": qty, "exit": position.exit_price if closed else None,
        "txn_cost_pct_per_side": fee, "slippage_pct_per_side": slip,
        "fees_paid": (entry+position.exit_price)*qty*fee if closed else entry*qty*fee,
        "marked_open_pnl": marked if not closed else 0,
        "open_liquidation_pnl": net if not closed else 0,
        "trailing_armed": armed or paper_armed,
        "pattern_trailing_armed": armed, "paper_trailing_armed": paper_armed,
        "bars": index, "gap_exit": gap_fill,
        "trailing_stop_basis": position.trailing_stop_basis,
        "paper_protection": position.paper_protection,
        "best_surviving_close_r": best_close_r,
        "ohlc_anomalies": anomalies,
        "coverage_exhausted": bool(counterfactual and trade.get("exit_reason") and not closed),
        "same_session_exit": closed and bar.timestamp == bars[0].timestamp,
        "entry_range_r": (bars[0].high-bars[0].low)*qty/risk if risk else None,
        "ambiguous_bars": ambiguous,
    }


def boundary_objective_comparison(trades: list[dict], **replay_options) -> dict:
    """Saved-entry diagnostic; never claim a rescan or alternative portfolio."""
    rows, excluded, targets = [], [], []
    for trade in trades:
        requested = trade.get("requested_rules") or {}
        resolved = trade.get("resolved_rules") or {}
        levels = {a["price"] for a in trade.get("chart_annotations", [])
                  if a.get("type") == "hline" and a.get("label") == "level"}
        if (trade.get("provenance") != "patternsv2" or len(levels) != 1
                or not requested.get("price") or not requested.get("take_profit")
                or not resolved.get("take_profit")):
            excluded.append({"trade_id": trade["trade_id"], "reason": "missing patternsv2 objective evidence"})
            continue
        if not math.isclose(requested["take_profit"], resolved["take_profit"], abs_tol=.0001, rel_tol=0):
            excluded.append({"trade_id": trade["trade_id"], "reason": "execution already changed the requested objective"})
            continue
        # The old adapter measured from the signal close. Keep the original
        # pattern height, shifting only that anchor to the saved boundary.
        target = resolved["take_profit"] + next(iter(levels)) - requested["price"]
        sign = -1 if trade["action"] == "SELL" else 1
        if target <= 0 or sign * (target - resolved["price"]) <= 0:
            excluded.append({"trade_id": trade["trade_id"], "reason": "objective exhausted before entry"})
            continue
        if sign * (target - resolved["take_profit"]) > .0001:
            excluded.append({"trade_id": trade["trade_id"], "reason": "boundary target is not tighter"})
            continue
        rows.append(replay_trade(trade, target_price=round(target, 4), **replay_options))
        targets.append({"trade_id": trade["trade_id"], "symbol": trade["symbol"],
                        "saved_target": resolved["take_profit"], "boundary_target": round(target, 4)})
    return {
        "scope": "same saved entries, quantities, costs and external exit decisions; observed OHLC only",
        "closed": summarize([row for row in rows if row["exit_reason"] != "open"]),
        "total_liquidation_pnl": sum(row["pnl"] for row in rows),
        "excluded": excluded, "targets": targets, "rows": rows,
    }


def _path_metrics(trades: list[dict], rows: list[dict]) -> dict:
    """Daily marked drawdown in currency, with both fees on closed trades."""
    by_id = {r["trade_id"]: r for r in rows}
    events = defaultdict(list)
    for t in trades:
        r = by_id[t["trade_id"]]
        for mark in t["daily_marks"]:
            pnl = mark["mtm"]
            if mark["sim_bar"] == r["exit_date"]:
                pnl = r["pnl"]
            events[mark["date"]].append((r["trade_id"], pnl))
    state, peak, drawdown, peak_open_count = {}, 0.0, 0.0, 0
    for date, changes in sorted(events.items()):
        state.update(changes)
        pnl = sum(state.values())
        peak = max(peak, pnl)
        drawdown = max(drawdown, peak-pnl)
        # Include entry-day positions before their intraday exits.
        peak_open_count = max(peak_open_count, sum(
            r["entry_date"][:10] <= date <= (r["exit_date"] or r["last_observed"])[:10]
            for r in rows))
    return {"daily_marked_max_drawdown": drawdown,
            "peak_concurrent_including_entry_day": peak_open_count,
            "last_marked_pnl": sum(state.values())}


def signal_funnel(logs: list[dict]) -> dict:
    """Count observed decisions once, retaining queued-to-fill/rejection outcomes.

    Logs are bounded evidence, not necessarily the full run. Sim-bar identity
    is the fallback for exports that predate durable signal IDs.
    """
    groups = defaultdict(list)
    for row in logs:
        if row.get("status") == "exited":
            continue
        identity = (row.get("signal_id") or (
            row.get("symbol"), row.get("timeframe"), row.get("pattern"),
            row.get("action"), row.get("sim_bar")))
        key = (row.get("run_id"), identity)
        groups[key].append(row)
    result = {}
    for side in ("BUY", "SELL"):
        decisions = [rows for rows in groups.values() if rows[0].get("action") == side]
        outcomes = Counter()
        for rows in decisions:
            if any(r["status"] == "filled" for r in rows):
                outcome = "filled"
            elif any(r["status"] == "rejected" for r in rows):
                reasons = [r.get("reason", "") for r in rows if r["status"] == "rejected"]
                outcome = ("shorts_disabled" if any(r.startswith("Short entries are disabled") for r in reasons)
                           else "DII" if any(r.startswith("DII") for r in reasons)
                           else "Kronos_oppose" if any(r.startswith("Kronos oppose:") for r in reasons)
                           else "Kronos_context_error" if any("Kronos forecast date differs" in r
                                or "Kronos forecast price differs" in r for r in reasons)
                           else "Kronos_worker_error" if any("Kronos worker error" in r
                                or "Kronos unavailable" in r or "Kronos busy" in r for r in reasons)
                           else "Kronos_no_opinion" if any(r.startswith("Kronos no_opinion:") for r in reasons)
                           else "opening_gap" if any(r.startswith("Opening gap") for r in reasons)
                           else "other_rejection")
            else:
                outcome = "queued_or_unresolved"
            outcomes[outcome] += 1
        result[side] = {"observed_decisions": len(decisions), "outcomes": dict(outcomes)}
    return result


def signal_evidence(book: dict) -> dict:
    """Keep cumulative scan counters separate from a possibly unrelated journal."""
    stats = book.get("scan_stats") or {}
    totals = stats.get("run_totals") or {}
    logs = book.get("signal_logs") or []
    start, end = stats.get("run_started_at"), stats.get("run_finished_at")
    within, outside, undated = [], [], []
    for row in logs:
        stamp = row.get("ts")
        if not stamp or not start or not end:
            undated.append(row)
        elif datetime.fromisoformat(start) <= datetime.fromisoformat(stamp) <= datetime.fromisoformat(end):
            within.append(row)
        else:
            outside.append(row)
    trades = book.get("closed_trades", []) + book.get("open_positions", [])
    fills = [r for r in logs if r.get("status") == "filled"]
    def matched(trade):
        return any(
            (trade.get("trade_id") and trade["trade_id"] == row.get("trade_id"))
            or (trade.get("signal_id") and trade["signal_id"] == row.get("signal_id"))
            for row in fills)
    missing = [t["trade_id"] for t in trades if not matched(t)]
    warnings = []
    if outside:
        warnings.append("Journal rows outside the saved scan run cannot explain that run's rejections.")
    if missing:
        warnings.append("Ledger trades lack matching filled journal identities; journal linkage is incomplete.")
    if totals and not within and logs:
        warnings.append("No journal rows are tied to the saved scan window; per-side rejection analysis is unavailable.")
    detected, rejected = totals.get("patterns_found"), totals.get("signals_rejected")
    return {
        "run_funnel": {
            "source": "scan_stats.run_totals; no missing decisions reconstructed",
            "detected": detected, "rejected": rejected,
            "filled": totals.get("trades_opened"),
            "rejection_by_gate": totals.get("rejection_by_gate"),
            "rejection_rate_pct": 100 * rejected / detected
            if detected and rejected is not None else None,
        },
        "journal": {
            "rows": len(logs), "rows_within_scan_window": len(within),
            "rows_outside_scan_window": len(outside),
            "rows_without_comparable_window": len(undated),
            "run_ids": sorted({r["run_id"] for r in logs if r.get("run_id")}),
            "ledger_trades_without_matching_fill_ids": missing,
        },
        "warnings": warnings,
    }


def review_export(path: Path, *, allow_inconsistent_ohlc: bool = False) -> dict:
    """Publish comparisons only after verifying every baseline trade in a book."""
    raw = path.read_bytes()
    export = json.loads(raw)
    result = {"export_sha256": hashlib.sha256(raw).hexdigest(),
              "exported_at": export["exported_at"], "engine_sha256": engine_hash(),
              "scope": "saved entries and OHLC through original exits/latest open marks; no rescanning",
              "books": []}
    for book in export["books"]:
        trades = book["closed_trades"] + book["open_positions"]
        if len({t["trade_id"] for t in trades}) != len(trades):
            raise ValueError("Duplicate trade identities in book")
        replay_options = {"allow_inconsistent_ohlc": allow_inconsistent_ohlc}
        rows = [replay_trade(t, **replay_options) for t in trades]
        if any(datetime.fromisoformat(r["last_observed"]) > datetime.fromisoformat(export["exported_at"])
               for r in rows):
            raise ValueError("Saved evidence extends beyond export time")
        closed = [r for r in rows if r["exit_reason"] != "open"]
        opened = [r for r in rows if r["exit_reason"] == "open"]
        if trades:
            metrics = book["metrics"]
            _equal(sum(r["pnl"] for r in closed), metrics["realized_pnl_dollars"], "realized total", .02)
            _equal(sum(r["marked_open_pnl"] for r in opened), metrics["unrealized_pnl_dollars"], "open total", .02)
            _equal(sum(r["fees_paid"] for r in rows), metrics["fees"], "total fees", .02)
        cap = [replay_trade(t, target_max_r=2, **replay_options) for t in trades]
        fixed = [replay_trade(t, trailing_stop_basis="entry", **replay_options) for t in trades]
        protected = [replay_trade(t, paper_loss_protection=True, **replay_options) for t in trades]
        anomalies = [{"trade_id": r["trade_id"], "symbol": r["symbol"], **anomaly}
                     for r in rows for anomaly in r["ohlc_anomalies"]]
        ranked = sorted(closed, key=lambda r: r["pnl"], reverse=True)
        def quarter(r):
            return r["entry_date"][:4] + "Q" + str((int(r["entry_date"][5:7])-1)//3+1)
        result["books"].append({
            "market": book["market"], "currency": book["currency"],
            "run_completion": {
                "termination_reason": (book.get("scan_stats") or {}).get("termination_reason"),
                "scan_complete": (book.get("scan_stats") or {}).get("scan_complete"),
                "sim_as_of": (book.get("execution_context") or {}).get("sim_as_of"),
                "execution_coverage": (book.get("execution_context") or {}).get("execution_coverage"),
            },
            "baseline_reproduced": True, "baseline_verified": not anomalies,
            "ohlc_anomalies": anomalies,
            "summary": summarize(closed),
            "closed_fees_paid": sum(r["fees_paid"] for r in closed),
            "closed_gross_pnl": sum(r["pnl"]+r["fees_paid"] for r in closed),
            "by_side": {s: summarize([r for r in closed if r["action"] == s]) for s in ("BUY", "SELL")},
            "by_pattern": {p: summarize([r for r in closed if r["pattern"] == p])
                           for p in sorted({r["pattern"] for r in closed})},
            "by_entry_price": {
                label: {s: summarize([r for r in closed if r["action"] == s and low <= r["entry"] < high])
                        for s in ("BUY", "SELL")}
                for label, low, high in (("below_5", 0, 5), ("5_to_10", 5, 10),
                                         ("10_to_50", 10, 50), ("50_and_above", 50, math.inf))
            },
            "by_entry_quarter": {q: {s: summarize([r for r in closed if quarter(r) == q and r["action"] == s])
                                     for s in ("BUY", "SELL")} for q in sorted({quarter(r) for r in closed})},
            "path": _path_metrics(trades, rows),
            "same_session": summarize([r for r in closed if r["same_session_exit"]]),
            "trailing_armed_count": sum(r["trailing_armed"] for r in rows),
            "ambiguous_exit_count": sum(bool(r["ambiguous_bars"]) for r in closed),
            "gap_exit_count": sum(r["gap_exit"] for r in closed),
            "paper_budget_breaches": [
                {"symbol": row["symbol"], "trade_id": row["trade_id"],
                 "net_return_pct": row["net_return_pct"], "gap_exit": row["gap_exit"]}
                for row in closed if row["paper_protection"] is not None
                and row["net_return_pct"] < -100 * row["paper_protection"]["loss_limit_pct"] - .000001
            ],
            "median_entry_range_r": median(r["entry_range_r"] for r in rows) if rows else None,
            "without_best_trade": summarize(ranked[1:]), "without_best_three": summarize(ranked[3:]),
            "signal_log_status_counts": dict(Counter(r["status"] for r in book.get("signal_logs", []))),
            "signal_funnel": signal_funnel(book.get("signal_logs", [])),
            "signal_funnel_scope": "exported journal grouped by run and decision; not inferred from scan counters",
            "signal_evidence": signal_evidence(book),
            "logged_rejections": dict(Counter(
                "DII" if r["reason"].startswith("DII") else
                "shorts_disabled" if r["reason"].startswith("Short entries are disabled") else
                "opening_gap" if r["reason"].startswith("Opening gap") else "other"
                for r in book.get("signal_logs", []) if r["status"] == "rejected")),
            "last_kronos_exit_decisions": dict(Counter(
                (t.get("kronos_exit_evidence") or {}).get("decision", "missing") for t in trades)),
            "open_marked_pnl": sum(r["marked_open_pnl"] for r in opened),
            "open_liquidation_pnl": sum(r["pnl"] for r in opened),
            "total_liquidation_pnl": sum(r["pnl"] for r in rows),
            "open_positions": opened,
            "targets_inside_trail_activation": [r["symbol"] for r in rows
                if r["target_r"] is not None and r["activation_r"] is not None
                and r["target_r"] <= r["activation_r"]],
            "comparisons": {
                "boundary_objectives": boundary_objective_comparison(trades, **replay_options),
                "target_cap_2r_closed": summarize([r for r in cap if r["exit_reason"] != "open"]),
                "target_cap_2r_total_liquidation_pnl": sum(r["pnl"] for r in cap),
                "long_only_closed": summarize([r for r in closed if r["action"] == "BUY"]),
                "long_only_total_liquidation_pnl": sum(r["pnl"] for r in rows if r["action"] == "BUY"),
                "entry_basis_trail": {
                    "closed": summarize([r for r in fixed if r["exit_reason"] != "open"]),
                    "coverage_exhausted": [r["symbol"] for r in fixed if r["coverage_exhausted"]],
                    # A looser exit can outlast the baseline's evidence. Do
                    # not present its earlier last close as the export cutoff.
                    "total_liquidation_pnl": None if any(r["coverage_exhausted"] for r in fixed)
                    else sum(r["pnl"] for r in fixed),
                },
                "paper_3pct_protection": {
                    "closed": summarize([r for r in protected if r["exit_reason"] != "open"]),
                    "coverage_exhausted": [r["symbol"] for r in protected if r["coverage_exhausted"]],
                    "total_liquidation_pnl": None if any(r["coverage_exhausted"] for r in protected)
                    else sum(r["pnl"] for r in protected),
                },
            },
            # Accounting stress only: hold fills, orders and quantity fixed.
            # This estimates extra friction, not a slippage-aware order replay.
            "extra_friction_per_side_stress": {
                str(rate): sum(r["pnl"] - (r["entry"]+r["exit"])*r["qty"]*rate for r in closed)
                for rate in (.0005, .001, .002)
            },
            "baseline": rows, "target_cap_2r": cap, "entry_basis_trail": fixed,
            "paper_3pct_protection": protected,
        })
    return result


def main() -> None:
    from utils.logger import set_console_level

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-inconsistent-ohlc", action="store_true",
                        help="Reproduce raw saved ranges, flag anomalies; never repair or certify them")
    args = parser.parse_args()
    set_console_level("ERROR")
    output = json.dumps(review_export(args.export, allow_inconsistent_ohlc=args.allow_inconsistent_ohlc),
                        indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
