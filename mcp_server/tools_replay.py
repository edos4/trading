"""Replay-payload tools (upload/load/clear a saved paper replay + render charts)."""

from __future__ import annotations

from typing import Any, Optional

from mcp_server import config as mcp_config
from mcp_server.api_client import get_client


def replay_load() -> dict:
    """Load the currently stored replay payload (or {"replay": null})."""
    return get_client().get("/api/replay/load")


def replay_upload(books: Any, extra: Optional[dict] = None) -> dict:
    """Persist a replay payload built from exported paper books (destructive)."""
    mcp_config.require_destructive()
    body: dict[str, Any] = dict(extra or {})
    body["books"] = books
    return get_client().post("/api/replay/upload", json=body)


def replay_clear() -> dict:
    """Delete the stored replay payload (destructive)."""
    mcp_config.require_destructive()
    return get_client().post("/api/replay/clear")


def replay_chart(
    symbol: str,
    market: Optional[str] = None,
    side: str = "open",
    action: Optional[str] = None,
    pattern: Optional[str] = None,
    pattern_version_id: Optional[str] = None,
    timeframe: str = "1d",
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    exit: Optional[float] = None,
    exit_reason: Optional[str] = None,
    current: Optional[float] = None,
    chart_annotations: Optional[list[dict]] = None,
    entry_time: Optional[str] = None,
    exit_time: Optional[str] = None,
) -> dict:
    """Build a trade-viewer chart payload for a replay/paper trade."""
    body: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "timeframe": timeframe,
    }
    for key, value in (
        ("market", market),
        ("action", action),
        ("pattern", pattern),
        ("pattern_version_id", pattern_version_id),
        ("entry", entry),
        ("stop", stop),
        ("target", target),
        ("exit", exit),
        ("exit_reason", exit_reason),
        ("current", current),
        ("chart_annotations", chart_annotations),
        ("entry_time", entry_time),
        ("exit_time", exit_time),
    ):
        if value is not None:
            body[key] = value
    return get_client().post("/api/replay/chart", json=body)
