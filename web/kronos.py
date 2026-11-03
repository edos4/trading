"""
web/kronos.py — GPU forecast bridge for the web UI's Kronos tab.

The app venv (``.venv``) is deliberately torch-free, so the model only resolves
inside the isolated ``.venv-kronos`` venv. This module shells out to
``scripts/kronos_web_forecast.py`` there, one run at a time (two model loads on
a 16 GB GPU must not overlap), and returns the parsed JSON payload. One run
handles every requested symbol — each model is loaded once and reused — so a
long comma-separated symbol list still costs just the two loads.
"""

from __future__ import annotations

from core.kronos_process import (
    KronosBusy,
    KronosError,
    KronosUnavailable,
    ROOT,
    available as _available,
    run_worker,
)

FORECAST_SCRIPT = ROOT / "scripts" / "kronos_web_forecast.py"

MAX_DAYS = 60
# Keep in sync with config.KRONOS_MAX_TRACKED_SYMBOLS (tracked-save fallback).
MAX_SYMBOLS = 100
TIMEOUT_S = 900


def available() -> bool:
    return _available(FORECAST_SCRIPT)


def top_symbols(n: int) -> list[str]:
    """Top ``n`` NASDAQ names by dollar volume (cached screener query).

    Backs the tab's "Top X NASDAQ" mode so the browser never talks to the
    screener itself. Returns fewer rows if the market has fewer names.
    """
    from core.market import US
    from data.tv_client import TVClient

    rows = TVClient.fetch_top_symbols_with_exchanges_cached(
        int(n), US.tv_screener, order_by="Value.Traded", exchange=US.tv_exchange,
    )
    return [sym for sym, _exchange in rows][: int(n)]


def forecast(symbols: list[str], days: int) -> dict:
    """Run base + fine-tuned forecasts for every symbol; return the JSON payload.

    Raises KronosUnavailable / KronosBusy / KronosError.
    """
    return run_worker(
        FORECAST_SCRIPT,
        ["--symbols", *symbols, "--days", str(int(days))],
        timeout_s=TIMEOUT_S,
        label="web",
    )
