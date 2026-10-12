"""Paper trading tools — control and inspect the US/PH paper books.

All of these proxy to the running web UI so a single process owns the paper
books and their ledgers.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp_server import config as mcp_config
from mcp_server.api_client import get_client


def paper_status(market: Optional[str] = None, lamps: bool = False) -> dict:
    """Paper book status.

    lamps=True returns only the lightweight running flags. market="us"/"ph"
    returns that book's full snapshot; omitted returns snapshot_all.
    """
    params: dict[str, Any] = {}
    if lamps:
        params["lamps"] = "true"
    if market:
        params["market"] = market
    return get_client().get("/api/paper/status", params=params)


def paper_start(
    market: str,
    n_symbols: int = 50,
    extra_symbols: str = "",
    use_stream: bool = False,
    stream_start: Optional[str] = None,
    pattern_only: bool = False,
    indicators_only: bool = False,
    collect_first: bool = False,
    collect_first_top_n: int = 4,
    kronos_gate: bool = True,
    kronos_rank: bool = False,
    kronos_batch: bool = False,
    volume_gate: bool = True,
) -> dict:
    """Start the paper trading scanner for one market ("us" or "ph").

    Returns 409 if that market's book is already running. Kronos gates are
    forced off by the server unless ENABLE_KRONOS=true.
    """
    body = {
        "market": market,
        "n_symbols": n_symbols,
        "extra_symbols": extra_symbols,
        "use_stream": use_stream,
        "stream_start": stream_start,
        "pattern_only": pattern_only,
        "indicators_only": indicators_only,
        "collect_first": collect_first,
        "collect_first_top_n": collect_first_top_n,
        "kronos_gate": kronos_gate,
        "kronos_rank": kronos_rank,
        "kronos_batch": kronos_batch,
        "volume_gate": volume_gate,
    }
    return get_client().post("/api/paper/start", json=body)


def paper_start_both(
    us: Optional[dict] = None,
    ph: Optional[dict] = None,
) -> dict:
    """Start both paper books at once.

    us/ph are optional PaperStartRequest dicts (same keys as paper_start, minus
    market). Returns {"ok", "errors"}.
    """
    body: dict[str, Any] = {}
    if us is not None:
        body["us"] = us
    if ph is not None:
        body["ph"] = ph
    return get_client().post("/api/paper/start-both", json=body)


def paper_stop(market: str = "all") -> dict:
    """Stop one paper book or all of them (market: "us", "ph", or "all")."""
    return get_client().post("/api/paper/stop", json={"market": market})


def paper_reset(market: str) -> dict:
    """Wipe a paper account and start from a fresh ledger (destructive)."""
    mcp_config.require_destructive()
    return get_client().post("/api/paper/reset", json={"market": market})


def paper_reset_logs(market: str = "all") -> dict:
    """Clear the paper signal logs (destructive; market: us, ph, or all)."""
    mcp_config.require_destructive()
    return get_client().post("/api/paper/reset-logs", json={"market": market})


def paper_export_trades(market: str = "all") -> dict:
    """Export open + closed paper trades as JSON (market: us, ph, or all)."""
    return get_client().get("/api/paper/export", params={"market": market})


def paper_trade_chart(
    market: str,
    side: str,
    symbol: Optional[str] = None,
    index: Optional[int] = None,
    log_time: Optional[str] = None,
    trade_id: Optional[str] = None,
) -> dict:
    """Render the chart for one paper trade (side: "open" or "closed")."""
    params: dict[str, Any] = {"market": market, "side": side}
    if symbol is not None:
        params["symbol"] = symbol
    if index is not None:
        params["index"] = index
    if log_time is not None:
        params["log_time"] = log_time
    if trade_id is not None:
        params["trade_id"] = trade_id
    return get_client().get("/api/paper/chart", params=params)
