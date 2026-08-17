"""Shared display helpers for paper/backtest trade blotters."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

EXIT_REASON_LABELS = {
    "stop_loss": "Stop",
    "take_profit": "Target",
    "trailing_stop": "Trail",
    "time_exit": "Time",
    "breakeven_stop": "BE",
}

_PATTERN_RE = re.compile(r"^pattern_(\d+)_(.+)$")


def format_exit_reason(
    reason: str | None,
    bars_elapsed: int | None = None,
    bars_configured: int | None = None,
) -> str:
    raw = (reason or "").strip()
    if not raw:
        return "—"
    label = EXIT_REASON_LABELS.get(raw, raw.replace("_", " "))
    if raw == "time_exit" and bars_elapsed is not None:
        if bars_configured is not None:
            return f"{label} {bars_elapsed}/{bars_configured}b"
        return f"{label} {bars_elapsed}b"
    return label


def reason_tone(reason: str | None) -> str:
    raw = (reason or "").strip()
    if raw in {"stop_loss"}:
        return "loss"
    if raw in {"take_profit", "trailing_stop"}:
        return "gain"
    if raw in {"breakeven_stop"}:
        return "flat"
    return "muted"


def format_pattern_name(name: str | None) -> str:
    raw = (name or "").strip()
    if not raw:
        return "—"
    m = _PATTERN_RE.match(raw)
    if m:
        return f"{m.group(1)} {m.group(2).replace('_', ' ')}"
    return raw.replace("_", " ")


def format_hold(days: float | None, bars: int | None) -> str:
    parts: list[str] = []
    if days is not None:
        if days < 1:
            parts.append(f"{days * 24:.1f}h")
        else:
            parts.append(f"{days:.1f}d")
    if bars is not None:
        parts.append(f"{bars}b")
    return " · ".join(parts) if parts else "—"


def format_stamp(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%m-%d %H:%M")


def pnl_dollars(trade: Any) -> float:
    qty = float(getattr(trade, "qty", 0) or 0)
    pnl = float(getattr(trade, "pnl", 0) or 0)
    return pnl * qty


def closed_book_stats(trades: list[Any]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "win_pct": 0.0,
            "last_n": 0,
            "last_wins": 0,
            "last_losses": 0,
            "last_r": 0.0,
            "last_pnl": 0.0,
        }

    def _dollars(t: Any) -> float:
        return pnl_dollars(t)

    wins = sum(1 for t in trades if _dollars(t) > 0)
    losses = sum(1 for t in trades if _dollars(t) < 0)
    last = sorted(
        trades,
        key=lambda t: getattr(t, "exit_date", None) or datetime.min,
        reverse=True,
    )[:10]
    last_r = 0.0
    try:
        from core.backtester import trade_r_multiple
    except Exception:
        trade_r_multiple = None
    for t in last:
        exit_px = getattr(t, "exit_price", None)
        if exit_px is None or trade_r_multiple is None:
            continue
        try:
            r = trade_r_multiple(t, exit_px)
        except Exception:
            r = None
        if r is not None:
            last_r += r
    return {
        "count": n,
        "wins": wins,
        "losses": losses,
        "flats": n - wins - losses,
        "win_pct": 100.0 * wins / n,
        "last_n": len(last),
        "last_wins": sum(1 for t in last if _dollars(t) > 0),
        "last_losses": sum(1 for t in last if _dollars(t) < 0),
        "last_r": last_r,
        "last_pnl": sum(_dollars(t) for t in last),
    }


def format_closed_stats(
    trades: list[Any],
    *,
    money: Optional[str] = None,
    showing: int | None = None,
) -> str:
    s = closed_book_stats(trades)
    if s["count"] == 0:
        return "No closed trades yet."
    last_pnl = money if money is not None else f"{s['last_pnl']:+,.2f}"
    line = (
        f"{s['count']} closed · {s['wins']}W / {s['losses']}L "
        f"({s['win_pct']:.0f}%) · last {s['last_n']}: "
        f"{s['last_wins']}W {s['last_losses']}L · "
        f"{s['last_r']:+.2f}R · {last_pnl}"
    )
    if showing is not None and showing != s["count"]:
        line += f" · showing {showing}"
    return line
