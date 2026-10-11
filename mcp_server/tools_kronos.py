"""Kronos forecast tool (returns 404 from the API when ENABLE_KRONOS=false)."""

from __future__ import annotations

from typing import Optional

from mcp_server.api_client import get_client


def kronos_predict(symbol: str, days: int = 3, market: Optional[str] = None) -> dict:
    """Forecast a symbol's close over the next ``days`` trading days.

    days is clamped by the API to 1..120. Requires ENABLE_KRONOS=true on the
    server and the local Kronos weights.
    """
    return get_client().post(
        "/api/kronos/predict",
        json={"symbol": symbol, "days": days, "market": market},
    )
