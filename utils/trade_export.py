"""Build a paper-trade dump meant to be sent to an LLM for strategy review."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from core.market import MARKET_PH, MARKET_US

BOOK_IDS = (MARKET_US, MARKET_PH)

REVIEW_PROMPT = """You are reviewing paper-trading results for a swing-trading bot
(chart patterns + optional Kronos 3d confirm gate (3% in 3 days) + optional volume gate).

Goal: recommend concrete rule changes that should improve expectancy and profit
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
        "daily_marks": [_daily_mark(m) for m in (row.get("daily_marks") or [])],
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
        "daily_marks": [_daily_mark(m) for m in (row.get("daily_marks") or [])],
    }


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
        "scan_stats": _scan_stats(snap.get("scan_stats")),
        "open_positions": [_open_row(p) for p in (snap.get("positions") or [])],
        "closed_trades": [_closed_row(t) for t in (snap.get("closed") or [])],
    }


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
    return {
        "purpose": "paper_trade_evaluation",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "filter": filt,
        "review_prompt": REVIEW_PROMPT.strip(),
        "notes": [
            "Open and closed paper trades from the operator desk.",
            "Each book is a separate ledger. Do not mix $ and ₱.",
            "Identity is (market, symbol), not ticker alone.",
        ],
        "books": books,
    }
