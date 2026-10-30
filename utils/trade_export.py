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
        "target": _round(row.get("target")),
        "pnl": _round(row.get("pnl")),
        "pnl_pct": _round(row.get("pnl_pct")),
        "r": _round(row.get("r")),
        "hold_days": _round(row.get("days")),
        "hold_bars": row.get("bars"),
        "exit_reason": row.get("reason"),
        "time_exit_bars_elapsed": row.get("time_exit_bars_elapsed"),
        "time_exit_bars_configured": row.get("time_exit_bars_configured"),
        "opened": row.get("opened"),
        "closed": row.get("closed"),
        "sim_opened": row.get("sim_opened"),
        "sim_closed": row.get("sim_closed"),
        "daily_marks": [_daily_mark(m) for m in (row.get("daily_marks") or [])],
        # Additive Laya evidence; legacy exports omit it ("unavailable").
        "laya_assessment": row.get("laya_assessment"),
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
    return out


def _scan_stats(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        return None
    keep = (
        "last_scan_at",
        "patterns_found",
        "trades_opened",
        "signals_rejected",
        "scan_duration_s",
        "sim_days",
        "rejection_by_gate",
    )
    out = {k: raw[k] for k in keep if k in raw}
    return out or None


def _book_export(snap: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(snap.get("metrics") or {})
    metrics.pop("equity_png_b64", None)
    return {
        "market": snap.get("market"),
        "label": snap.get("label"),
        "currency": snap.get("currency"),
        "currency_symbol": snap.get("currency_symbol"),
        "long_only": snap.get("long_only"),
        "session": snap.get("session"),
        "running": bool(snap.get("running")),
        "cash": _round(snap.get("cash"), 2),
        "equity": _round(snap.get("equity"), 2),
        "open_count": snap.get("open_count"),
        "closed_count": snap.get("closed_count"),
        "exposure": snap.get("exposure"),
        "metrics": metrics,
        "summary": snap.get("summary"),
        "account_model": "independent-fixed-notional",
        "pending_entries": snap.get("pending_entries", []),
        "order_events": snap.get("order_events", []),
        "scan_stats": _scan_stats(snap.get("scan_stats")),
        "signal_logs": [_signal_row(s) for s in (snap.get("signal_logs") or [])],
        "open_positions": [_open_row(p) for p in (snap.get("positions") or [])],
        "closed_trades": [_closed_row(t) for t in (snap.get("closed") or [])],
    }


def snapshot_from_paper_account(
    account,
    *,
    scan_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PaperBook-shaped snapshot so CLI dumps reuse the same export schema."""
    from core.paper_trader import (
        bars_held,
        days_held,
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
                "target": p.take_profit,
                "opened": p.entry_date.isoformat() if p.entry_date else "",
                "sim_opened": p.sim_entry_date.isoformat() if p.sim_entry_date else None,
                "timeframe": p.timeframe,
                "daily_marks": list(p.position_marks or []),
                "laya_assessment": (
                    dict(p.laya_assessment)
                    if getattr(p, "laya_assessment", None) else None),
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
                "days": days_held(t),
                "bars": bars_held(t),
                "time_exit_bars_elapsed": t.time_exit_bars_elapsed,
                "time_exit_bars_configured": t.exit_bars_after_neckline_break,
                "reason": t.exit_reason,
                "stop": t.stop_loss,
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
        "account_model": "independent-fixed-notional",
        "pending_entries": [{"symbol": s.symbol, "pattern": s.pattern,
                             "decision_date": s.signal_bar_timestamp.isoformat()} for s in account.pending_entries],
        "order_events": list(account.order_events),
        "cash": snap["cash"],
        "equity": equity,
        "open_count": len(snap["positions"]),
        "closed_count": len(snap["closed"]),
        "exposure": snap["exposure"],
        "scan_stats": scan_stats,
        "signal_logs": [
            {**row, "market": row.get("market") or profile.id}
            for row in reversed(load_signal_log(profile.id))
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
) -> dict[str, Any]:
    """CLI paper dump: same JSON as the UI Export Trades button, one book."""
    snap = snapshot_from_paper_account(account, scan_stats=scan_stats)
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
            "signal_logs holds every detected signal with its accept/fill/reject"
            " outcome and reason, newest first, as shown in the Paper Logs tab.",
            "Each book is a separate ledger. Do not mix $ and ₱.",
            "Identity is (market, symbol), not ticker alone.",
        ],
        "books": books,
    })
