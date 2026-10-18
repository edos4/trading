"""Backtest tools (durable shared-service runs + the single-slot quick job)."""

from __future__ import annotations

from typing import Any, Optional

from mcp_server.api_client import get_client


def backtest_quick_status() -> dict:
    """Status of the single-slot in-process quick backtest job."""
    return get_client().get("/api/backtest/status")


def backtest_quick_run(
    pattern: Optional[str] = None,
    market: Optional[str] = None,
    universe: Optional[str] = None,
    symbols: Optional[str] = None,
    txn_cost_pct: Optional[float] = None,
    volume_gate: Optional[bool] = None,
    pattern_only: Optional[bool] = None,
) -> dict:
    """Start the single-slot quick backtest (returns 409 if one is already busy).

    Only the fields provided are sent; the server fills defaults.
    """
    form: dict[str, Any] = {}
    if pattern is not None:
        form["pattern"] = pattern
    if market is not None:
        form["market"] = market
    if universe is not None:
        form["universe"] = universe
    if symbols is not None:
        form["extra_symbols"] = symbols
    if txn_cost_pct is not None:
        form["txn_cost_pct"] = txn_cost_pct
    if volume_gate is not None:
        form["volume_gate"] = volume_gate
    if pattern_only is not None:
        form["pattern_only"] = pattern_only
    return get_client().post("/api/backtest/run", json=form)


def backtest_catalog() -> dict:
    """List patterns registered for backtesting."""
    return get_client().get("/api/backtest/catalog")


def backtest_versions(pattern_id: str, include_archived: bool = False) -> dict:
    """List the versions of one pattern available to backtest."""
    return get_client().get(
        f"/api/backtest/catalog/{pattern_id}/versions",
        params={"include_archived": include_archived},
    )


def list_backtest_presets() -> dict:
    """List saved backtest presets."""
    return get_client().get("/api/backtest/presets")


def save_backtest_preset(
    name: str,
    settings: dict,
    preset_id: Optional[str] = None,
    expected_generation: Optional[int] = None,
) -> dict:
    """Create or update a named backtest preset from a settings dict."""
    body: dict[str, Any] = {"name": name, "settings": settings}
    if preset_id is not None:
        body["preset_id"] = preset_id
    if expected_generation is not None:
        body["expected_generation"] = expected_generation
    return get_client().post("/api/backtest/presets", json=body)


def submit_backtest_run(
    mode: str = "offline",
    market: str = "us",
    symbols: Optional[list[str]] = None,
    universe: Optional[str] = None,
    versions: Optional[dict[str, str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    session_count: int = 0,
    warmup_bars: int = 40,
    initial_capital: float = 100_000.0,
    sizing_mode: str = "fixed-notional",
    position_notional: float = 10_000.0,
    txn_cost_pct: float = 0.0,
    slippage_pct: float = 0.0005,
    pattern_only: bool = False,
    volume_gate: bool = False,
    kronos_gate: bool = False,
    kronos_rank: bool = False,
    collect_first: int = 0,
    end_policy: str = "keep-open",
    preset_id: Optional[str] = None,
    preset_name: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    evaluation: Optional[dict[str, Any]] = None,
) -> dict:
    """Submit a durable shared-service backtest run; returns {"run_id", "state"}.

    mode is "offline" or "historical-stream". Poll with get_backtest_run.
    Repeat the same idempotency_key to safely retry a submission.
    """
    body: dict[str, Any] = {
        "mode": mode,
        "market": market,
        "symbols": symbols or [],
        "session_count": session_count,
        "warmup_bars": warmup_bars,
        "initial_capital": initial_capital,
        "sizing_mode": sizing_mode,
        "position_notional": position_notional,
        "txn_cost_pct": txn_cost_pct,
        "slippage_pct": slippage_pct,
        "pattern_only": pattern_only,
        "volume_gate": volume_gate,
        "kronos_gate": kronos_gate,
        "kronos_rank": kronos_rank,
        "collect_first": collect_first,
        "end_policy": end_policy,
    }
    for key, value in (
        ("universe", universe),
        ("versions", versions),
        ("start_date", start_date),
        ("end_date", end_date),
        ("preset_id", preset_id),
        ("preset_name", preset_name),
        ("idempotency_key", idempotency_key),
        ("evaluation", evaluation),
    ):
        if value is not None:
            body[key] = value
    return get_client().post("/api/backtest/runs", json=body)


def get_backtest_run(run_id: str) -> dict:
    """Fetch the payload/state of a durable backtest run."""
    return get_client().get(f"/api/backtest/runs/{run_id}")


def cancel_backtest_run(run_id: str) -> dict:
    """Request cancellation of a durable backtest run (cooperative)."""
    return get_client().post(f"/api/backtest/runs/{run_id}/cancel")


def retry_backtest_run(run_id: str) -> dict:
    """Retry a terminal durable backtest run; returns {"run_id", "state"}."""
    return get_client().post(f"/api/backtest/runs/{run_id}/retry")
