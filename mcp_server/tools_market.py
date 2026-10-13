"""Market data & explorer tools (proxy to the web UI ``/api/*`` surface)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from mcp_server import config as mcp_config
from mcp_server.api_client import TradingApiError, get_client


def server_health() -> dict:
    """Check the trading web service is reachable and authenticated.

    Returns {"ok": true, ...} when the proxy target answers, else a populated
    error describing the failure. Always succeeds itself (never raises).
    """
    try:
        return {"ok": True, "response": get_client().get("/health")}
    except TradingApiError as exc:
        return {
            "ok": False,
            "status_code": exc.status_code,
            "detail": exc.detail,
        }


def get_markets() -> dict:
    """List the available market profiles (us, ph) and the default market."""
    return get_client().get("/api/markets")


def list_symbols(n: int = 50, market: Optional[str] = None) -> dict:
    """Top-N symbols from the live TradingView **screener** universe (discovery only).

    This is a dynamic scan list, NOT the set the engine can backtest. For the
    daily OHLCV history store — every ticker available to backtests and paper
    trading (thousands of US + PH symbols, including large caps like AAPL) — use
    ``get_history_symbols``. n is clamped by the API to 5..5000; market is "us"
    (default) or "ph".
    """
    return get_client().get("/api/symbols", params={"n": n, "market": market or ""})


def get_history_symbols(market: Optional[str] = None) -> dict:
    """Symbols available in the daily OHLCV history store (with last bar)."""
    return get_client().get("/api/history/symbols", params={"market": market or ""})


def get_history_bars(
    symbol: str,
    market: Optional[str] = None,
    after_ts: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    """Daily OHLCV bars for one symbol.

    ``limit`` is clamped to ``MCP_MAX_HISTORY_BARS`` (default 2000) to bound the
    response; when omitted the cap is used.
    """
    cap = mcp_config.max_history_bars()
    params: dict = {"market": market or ""}
    if after_ts is not None:
        params["after_ts"] = int(after_ts)
    params["limit"] = cap if limit is None else max(1, min(int(limit), cap))
    return get_client().get(f"/api/history/{symbol}", params=params)


def get_history_meta(symbol: str, market: Optional[str] = None) -> dict:
    """Metadata for one history symbol (row count, first/last bar)."""
    return get_client().get(f"/api/history/{symbol}/meta", params={"market": market or ""})


def analyze_symbol(
    symbol: str,
    exchange: str = "",
    timeframe: str = "1d",
    run_patterns: bool = True,
    indicators_only: bool = False,
    kronos_gate: Optional[bool] = None,
    kronos_batch: Optional[bool] = None,
    volume_gate: Optional[bool] = None,
    market: Optional[str] = None,
    save_chart: Optional[str] = None,
) -> dict:
    """Fetch candles for a symbol and run every pattern; returns signals + chart.

    timeframe is "1d" or "1W". When ``save_chart`` is a path, the chart PNG is
    written there (a directory gets ``<SYMBOL>_<timeframe>.png``) and the base64
    blob is dropped in favour of ``chart_png_path`` to keep the response small.
    """
    body = {
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "run_patterns": run_patterns,
        "indicators_only": indicators_only,
        "kronos_gate": kronos_gate,
        "kronos_batch": kronos_batch,
        "volume_gate": volume_gate,
        "market": market,
    }
    result = get_client().post("/api/symbol", json=body)
    if save_chart and isinstance(result, dict) and result.get("chart_png_b64"):
        out = Path(save_chart).expanduser()
        if out.is_dir():
            out = out / f"{symbol.upper().strip()}_{timeframe}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(result["chart_png_b64"]))
        result = dict(result)
        result.pop("chart_png_b64", None)
        result["chart_png_path"] = str(out)
    return result
