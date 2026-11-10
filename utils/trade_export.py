"""Build a paper-trade dump meant to be sent to an LLM for strategy review."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from typing import Any

from core.market import MARKET_PH, MARKET_US, clock_payload, get_market
from utils.json_values import json_safe

BOOK_IDS = (MARKET_US, MARKET_PH)

REVIEW_PROMPT = """Goal: recommend concrete rule changes that should improve expectancy and profit
factor without collapsing trade count to zero.

Rules:
- Treat each book separately. Never combine USD ($) and PHP (₱) into one P&L.
- Tickers are (market, symbol). SM, AC, TEL and others exist on both tapes.
- Judge by R-multiple, exit-reason mix, hold time, pattern, side, and sample size.
- Open positions include daily_marks (one row per session close while held).
- Call out overfitting. A handful of trades is not an edge.
- Prefer changes the operator can actually ship (pattern filters, gate knobs,
  stops/targets, time-exits, universe size) over vague advice.
"""


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return round(value, digits)
    return value


def _open_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market"),
        "symbol": row.get("symbol"),
        "status": row.get("status"),
        "action": row.get("action"),
        "pattern": row.get("pattern"),
        **__import__("core.pattern_provenance", fromlist=["row_payload"]).row_payload(row),
        "chart_annotations": deepcopy(row.get("chart_annotations") or []),
        "timeframe": row.get("timeframe"),
        "qty": _round(row.get("qty")),
        "entry": _round(row.get("entry")),
        "current": _round(row.get("current")),
        "stop": _round(row.get("stop")),
        "exit_state": deepcopy(row.get("exit_state")),
        "target": _round(row.get("target")),
        "unrealized_pct": _round(row.get("unrl_pct")),
        "mtm": _round(row.get("mtm")),
        "r": _round(row.get("r")),
        "hold_days": _round(row.get("days")),
        "hold_bars": row.get("bars"),
        "value": _round(row.get("value")),
        "port_pct": _round(row.get("port_pct")),
        "risk": _round(row.get("risk")),
        "opened": row.get("opened"),
        "sim_opened": row.get("sim_opened"),
        "daily_marks": [_daily_mark(m) for m in (row.get("daily_marks") or [])],
        # Additive Laya evidence (docs/laya.md P5). Older snapshots omit it;
        # Replay renders those as "Laya assessment unavailable" and never
        # reruns today's model when viewing.
        "laya_assessment": row.get("laya_assessment"),
        "kronos_evidence": row.get("kronos_evidence"),
        "kronos_exit_evidence": row.get("kronos_exit_evidence"),
    }


def _daily_mark(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": row.get("date"),
        "sim_bar": row.get("sim_bar"),
        "close": _round(row.get("close")),
        "unrl_pct": _round(row.get("unrl_pct")),
        "mtm": _round(row.get("mtm"), 2),
        "r": _round(row.get("r")),
        "value": _round(row.get("value"), 2),
        "status": row.get("status"),
        "bars": row.get("bars"),
        "stop": _round(row.get("stop")),
        "target": _round(row.get("target")),
        "exit_state": deepcopy(row.get("exit_state")),
        "exit_check": deepcopy(row.get("exit_check")),
    }


def _closed_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market"),
        "symbol": row.get("symbol"),
        "action": row.get("action"),
        "pattern": row.get("pattern"),
        **__import__("core.pattern_provenance", fromlist=["row_payload"]).row_payload(row),
        "chart_annotations": deepcopy(row.get("chart_annotations") or []),
        "timeframe": row.get("timeframe"),
        "qty": _round(row.get("qty")),
        "entry": _round(row.get("entry")),
        "exit": _round(row.get("exit")),
        "stop": _round(row.get("stop")),
        "exit_state": deepcopy(row.get("exit_state")),
        "target": _round(row.get("target")),
        "pnl": _round(row.get("pnl")),
        "pnl_pct": _round(row.get("pnl_pct")),
        "r": _round(row.get("r")),
        "net_r": _round(row.get("net_r")),
        "hold_days": _round(row.get("days")),
        "hold_bars": row.get("bars"),
        "exit_reason": row.get("reason"),
        "kronos_exit": row.get("reason") == "kronos_exit",
        "time_exit_bars_elapsed": row.get("time_exit_bars_elapsed"),
        "time_exit_bars_configured": row.get("time_exit_bars_configured"),
        "opened": row.get("opened"),
        "closed": row.get("closed"),
        "sim_opened": row.get("sim_opened"),
        "sim_closed": row.get("sim_closed"),
        "daily_marks": [_daily_mark(m) for m in (row.get("daily_marks") or [])],
        # Additive Laya evidence; legacy exports omit it ("unavailable").
        "laya_assessment": row.get("laya_assessment"),
        "kronos_evidence": row.get("kronos_evidence"),
        "kronos_exit_evidence": row.get("kronos_exit_evidence"),
    }


SIGNAL_LOG_FIELDS = (
    "market", "ts", "sim_bar", "sim_bar_idx", "symbol", "timeframe", "action",
    "pattern", "confidence", "price", "status", "reason",
)


def _signal_row(row: dict[str, Any]) -> dict[str, Any]:
    """Log-tab signal: the decision fields plus provenance, no chart geometry."""
    out = {key: _round(row.get(key)) for key in SIGNAL_LOG_FIELDS}
    out.update(
        __import__("core.pattern_provenance", fromlist=["row_payload"]).row_payload(row))
    # Only present on Kronos-tagged rows, so legacy log exports stay unchanged.
    for key in ("kronos_evidence", "kronos_exit_evidence", "exit_reason", "decision_context", "run_id"):
        value = row.get(key)
        if value is not None:
            out[key] = value
    return out


def _scan_stats(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        return None
    keep = set('''last_scan_at patterns_found trades_opened signals_rejected
        scan_duration_s sim_days rejection_by_gate scan_started_at scan_finished_at
        scan_in_progress run_started_at run_finished_at scan_total_s scans_completed
        run_totals termination_reason scan_complete symbols_total symbols_with_snapshot
        snapshot_errors timeframe_requests timeframe_snapshots_ok new_bars pattern_evaluations
        daily_date_skew daily_dates_seen asof_skipped dead_symbols held_coverage_errors
        invalid_bar_skipped entry_symbols_total execution_watchlist_pairs
        analysis_errors volume_gate_rejected dii_rejected dii_shadow kronos_rejected
        kronos_exit_shadow kronos_exits kronos_exit_deferred collect_first_ranked
        collect_first_selected rule_filter_shadow rule_filter_rejected rule_filter_prefiltered
        short_rejected geometry_rejected price_floor_rejected orders_queued
        laya_shadow laya_filtered kronos_deferred'''.split())
    return deepcopy({k:v for k,v in raw.items() if k in keep})


def _book_export(snap: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(snap.get("metrics") or {})
    metrics.pop("equity_png_b64", None)
    execution_context = deepcopy(snap.get("execution_context"))
    return {
        "market": snap.get("market"),
        "label": snap.get("label"),
        "currency": snap.get("currency"),
        "currency_symbol": snap.get("currency_symbol"),
        "long_only": snap.get("long_only"),
        "session": snap.get("session"),
        "running": bool(snap.get("running")),
        # The desk header verbatim, plus the same filters as structured JSON,
        # so a review knows which gates produced these trades.
        "status": snap.get("status"),
        "settings": dict(snap.get("settings") or {}),
        "execution_context": execution_context,
        "use_stream": snap.get("use_stream"),
        "duration_days": snap.get("duration_days"),
        "laya": snap.get("laya"),
        "cash": _round(snap.get("cash"), 2),
        "equity": _round(snap.get("equity"), 2),
        "open_count": snap.get("open_count"),
        "closed_count": snap.get("closed_count"),
        "exposure": snap.get("exposure"),
        "metrics": metrics,
        "summary": snap.get("summary"),
        "account_model": (snap.get('execution_context') or {}).get('account_model', 'independent-fixed-notional'),
        "pending_entries": snap.get("pending_entries", []),
        "order_events": snap.get("order_events", []),
        "scan_stats": _scan_stats(snap.get("scan_stats")),
        "scan_timing": snap.get("scan_timing"),
        "signal_logs": [_signal_row(s) for s in (snap.get("signal_logs") or [])],
        "open_positions": [_open_row(p) for p in (snap.get("positions") or [])],
        "closed_trades": [_closed_row(t) for t in (snap.get("closed") or [])],
    }


def snapshot_from_paper_account(
    account,
    *,
    scan_stats: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """PaperBook-shaped snapshot so CLI dumps reuse the same export schema."""
    from core.paper_trader import (
        bars_held,
        days_held,
        exit_state,
        position_status,
        r_multiple,
        risk_dollars,
        sim_days_held,
        unrealized_pct,
    )
    from core.signal_log_store import load_signal_log

    profile = get_market(account.market)
    clock = clock_payload(profile.id)
    now = datetime.now(timezone.utc)
    snap = account.snapshot_metrics()
    equity = snap["equity"]
    result = account.to_result()

    positions = []
    for sym, p, current, mtm in snap["positions"]:
        r = r_multiple(p, current)
        value = current * p.qty
        positions.append(
            {
                "market": profile.id,
                "symbol": sym,
                "status": position_status(p),
                "action": p.action,
                "pattern": p.pattern,
                    **__import__("core.pattern_provenance", fromlist=["payload"]).payload(p),
                "chart_annotations": deepcopy(p.chart_annotations or []),
                "qty": p.qty,
                "entry": p.entry_price,
                "current": current,
                "unrl_pct": unrealized_pct(p, current),
                "r": r,
                "days": sim_days_held(p, account.sim_now() or now),
                "bars": bars_held(p, account.bar_count(sym)),
                "value": value,
                "mtm": mtm,
                "port_pct": (value / equity * 100) if equity > 0 else 0.0,
                "risk": risk_dollars(p),
                "stop": p.stop_loss,
                "exit_state": exit_state(p),
                "target": p.take_profit,
                "opened": p.entry_date.isoformat() if p.entry_date else "",
                "sim_opened": p.sim_entry_date.isoformat() if p.sim_entry_date else None,
                "timeframe": p.timeframe,
                "daily_marks": list(p.position_marks or []),
                "laya_assessment": (
                    dict(p.laya_assessment)
                    if getattr(p, "laya_assessment", None) else None),
                "kronos_evidence": (
                    dict(p.kronos_evidence)
                    if getattr(p, "kronos_evidence", None) else None),
                "kronos_exit_evidence": (
                    dict(p.kronos_exit_evidence)
                    if getattr(p, "kronos_exit_evidence", None) else None),
            }
        )

    closed = []
    for t in snap["closed"]:
        exit_px = t.exit_price if t.exit_price is not None else t.entry_price
        closed.append(
            {
                "market": profile.id,
                "symbol": t.symbol,
                "action": t.action,
                "pattern": t.pattern,
                    **__import__("core.pattern_provenance", fromlist=["payload"]).payload(t),
                "chart_annotations": deepcopy(t.chart_annotations or []),
                "qty": t.qty,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "pnl_pct": t.pnl_pct,
                "pnl": t.pnl * t.qty,
                "r": r_multiple(t, exit_px),
                "net_r": t.pnl_usd / risk_dollars(t) if risk_dollars(t) else None,
                "days": days_held(t),
                "bars": bars_held(t),
                "time_exit_bars_elapsed": t.time_exit_bars_elapsed,
                "time_exit_bars_configured": t.exit_bars_after_neckline_break,
                "reason": t.exit_reason,
                "kronos_exit": t.exit_reason == "kronos_exit",
                "stop": t.stop_loss,
                "exit_state": exit_state(t),
                "target": t.take_profit,
                "opened": t.entry_date.isoformat() if t.entry_date else "",
                "closed": t.exit_date.isoformat() if t.exit_date else "",
                "sim_opened": t.sim_entry_date.isoformat() if t.sim_entry_date else None,
                "sim_closed": t.sim_exit_date.isoformat() if t.sim_exit_date else None,
                "timeframe": t.timeframe,
                "daily_marks": list(t.position_marks or []),
                "laya_assessment": (
                    dict(t.laya_assessment)
                    if getattr(t, "laya_assessment", None) else None),
                "kronos_evidence": (
                    dict(t.kronos_evidence)
                    if getattr(t, "kronos_evidence", None) else None),
                "kronos_exit_evidence": (
                    dict(t.kronos_exit_evidence)
                    if getattr(t, "kronos_exit_evidence", None) else None),
            }
        )

    total_pnl = snap["total_pnl_dollars"]
    return {
        "market": profile.id,
        "label": profile.label,
        "currency": profile.currency,
        "currency_symbol": profile.currency_symbol,
        "long_only": profile.long_only,
        "session": clock["session"],
        "running": False,
        "status": status,
        "settings": dict(settings or {}),
        "execution_context": snap["execution_context"],
        "use_stream": bool((settings or {}).get("use_stream")),
        "duration_days": (settings or {}).get("duration_days"),
        "laya": {
            "mode": (settings or {}).get("laya_mode") or "off",
            "policy_id": (settings or {}).get("laya_policy_id") or "",
        },
        "account_model": account.account_model,
        "pending_entries": [{"symbol": s.symbol, "pattern": s.pattern,
                             "decision_date": s.signal_bar_timestamp.isoformat()} for s in account.pending_entries],
        "order_events": list(account.order_events),
        "cash": snap["cash"],
        "equity": equity,
        "open_count": len(snap["positions"]),
        "closed_count": len(snap["closed"]),
        "exposure": snap["exposure"],
        "scan_stats": scan_stats,
        # Cumulative across every run of this book (persisted on the account).
        "scan_timing": account.scan_clock(),
        "signal_logs": [
            {**row, "market": row.get("market") or profile.id}
            for row in reversed(load_signal_log(profile.id, limit=0))
        ],
        "positions": positions,
        "closed": closed,
        "summary": result.summary() if result.trades else "No closed trades yet.",
        "metrics": {
            "total_pnl_dollars": total_pnl,
            "fees": snap["fees"],
            "total_pnl_pct": (total_pnl / snap["initial_capital"] * 100)
            if snap["initial_capital"] else 0.0,
            "realized_pnl_dollars": snap["realized_pnl_dollars"],
            "unrealized_pnl_dollars": snap["unrealized_pnl_dollars"],
            "win_rate_pct": result._s["win_rate_pct"],
            "avg_pnl_pct": result._s["avg_pnl_pct"],
            "total_usd": result._s["total_usd"],
            "worst_usd": result._s["worst_usd"],
            "profit_factor": result._s["profit_factor"],
            "avg_hold_bars": result.avg_hold_bars,
            "exit_reason_breakdown": result.exit_reason_breakdown,
        },
    }


def build_paper_account_export(
    account,
    *,
    scan_stats: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """CLI paper dump: same JSON as the UI Export Trades button, one book."""
    snap = snapshot_from_paper_account(
        account,
        scan_stats=scan_stats,
        settings=settings,
        status=status,
    )
    return build_paper_trade_export(
        {"books": {snap["market"]: snap}},
        market=snap["market"],
    )


def build_paper_trade_export(
    envelope: dict[str, Any],
    *,
    market: str | None = None,
) -> dict[str, Any]:
    """JSON payload: open + closed trades, split by book, ready for LLM review."""
    filt = (market or "all").strip().lower()
    if filt in ("", "all"):
        ids = list(BOOK_IDS)
        filt = "all"
    elif filt in BOOK_IDS:
        ids = [filt]
    else:
        raise ValueError("market must be us, ph, or all")

    books_in = envelope.get("books") or {}
    books = [_book_export(books_in[mid]) for mid in ids if mid in books_in]
    return json_safe({
        "purpose": "paper_trade_evaluation",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "filter": filt,
        "review_prompt": REVIEW_PROMPT.strip(),
        "notes": [
            "Open and closed paper trades from the operator desk.",
            "Each book's `status` is the desk header verbatim and `settings`"
            " carries the same run filters as structured fields (market,"
            " symbols, pace, stream start, volume gate, pattern-only,"
            " indicators-only, patternsv2, collect-first, Laya, session).",
            "signal_logs holds every detected signal with its accept/fill/reject"
            " outcome and reason, plus status='exited' rows for Kronos closes,"
            " newest first, as shown in the Paper Logs tab.",
            "closed_trades[].kronos_exit is true only for an executed"
            " kronos_exit. An opposing vote in kronos_exit_evidence can be"
            " deferred by the adverse-move guard without closing the trade.",
            "Each book is a separate ledger. Do not mix $ and ₱.",
            "Identity is (market, symbol), not ticker alone.",
            "execution_context records current account costs, slippage, simulated"
            " clock and entry settings at snapshot time. Historical trades use"
            " their own requested_rules/resolved_rules; missing context in older"
            " snapshots is unknown, not today's defaults.",
            "stop is the saved pattern/override level; exit_state.hard_stop"
            " includes the entry cap. Trailing stops are separate orders."
            " paper_protection saves the independent cost-aware 3% net loss"
            " budget and entry-notional giveback; exit_state.paper_protective_stop"
            " is its current resting trigger. Live last_quote and quote_ratchets"
            " retain observed-price trail evidence. Gaps can exceed the budget.",
            "r is gross price movement / initial risk; net_r includes both"
            " transaction fees and executed slippage for closed trades.",
            "daily_marks[].close is the exit fill on the final row. New marks"
            " include exit_check.ohlc and orders_before_bar, distinct from"
            " exit_state (the state after checking this bar). Older marks"
            " have no reconstructed exit evidence.",
            "exit_check.observation_kind='live_quote' records a forming-bar exit"
            " at the observed last price; ohlc is a point observation, not a"
            " completed candle. source_bar identifies the forming candle."
            " A daily-bar replay cannot certify these intraday fills.",
            "After live quote management, observation_kind='live_bar_close' keeps"
            " the full completed ohlc for pattern close decisions and resting_ohlc"
            " as the observed close for resting orders. Earlier daily ranges"
            " cannot execute an order tightened later in that bar.",
        ],
        "books": books,
    })
