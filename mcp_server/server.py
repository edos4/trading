"""MCP server entry — registers the trading tools and runs over stdio.

Run with ``python main.py --mcp`` or ``python -m mcp_server``. The agent spawns
this process; it proxies to the running web UI (``python main.py --web``).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server import (
    tools_backtest,
    tools_deploy,
    tools_kronos,
    tools_maintenance,
    tools_market,
    tools_paper,
    tools_patterns,
    tools_replay,
)

INSTRUCTIONS = (
    "Operate and inspect the trading bot. Tools proxy to the running web UI "
    "(python main.py --web), which owns the paper books and backtest jobs — so "
    "that service must be up. There is no live order path: paper and backtest "
    "only. Destructive/maintenance/deploy groups are default-deny and require "
    "MCP_ALLOW_* flags in .env."
)

mcp = FastMCP(name="trading-bot", instructions=INSTRUCTIONS)

# Grouped so the registry is easy to audit and test.
TOOL_GROUPS: dict[str, list] = {
    "market": [
        tools_market.server_health,
        tools_market.get_markets,
        tools_market.list_symbols,
        tools_market.get_history_symbols,
        tools_market.get_history_bars,
        tools_market.get_history_meta,
        tools_market.analyze_symbol,
    ],
    "patterns": [
        tools_patterns.list_patterns,
        tools_patterns.get_patterns_pinned,
        tools_patterns.get_patterns_bundle,
        tools_patterns.list_pattern_versions,
        tools_patterns.get_pattern_version,
        tools_patterns.get_pattern_version_source,
        tools_patterns.diff_pattern_version,
        tools_patterns.get_pattern_version_bundle,
        tools_patterns.get_pattern_provider,
        tools_patterns.submit_pattern_edit,
        tools_patterns.get_pattern_edit_job,
        tools_patterns.cancel_pattern_edit,
        tools_patterns.retry_pattern_edit,
        tools_patterns.backtest_pattern_version,
        tools_patterns.get_pattern_run,
        tools_patterns.get_pattern_run_chart,
        tools_patterns.set_pattern_default,
        tools_patterns.archive_pattern_version,
    ],
    "backtest": [
        tools_backtest.backtest_quick_status,
        tools_backtest.backtest_quick_run,
        tools_backtest.backtest_catalog,
        tools_backtest.backtest_versions,
        tools_backtest.list_backtest_presets,
        tools_backtest.save_backtest_preset,
        tools_backtest.submit_backtest_run,
        tools_backtest.get_backtest_run,
        tools_backtest.cancel_backtest_run,
        tools_backtest.retry_backtest_run,
    ],
    "paper": [
        tools_paper.paper_status,
        tools_paper.paper_start,
        tools_paper.paper_start_both,
        tools_paper.paper_stop,
        tools_paper.paper_reset,
        tools_paper.paper_reset_logs,
        tools_paper.paper_export_trades,
        tools_paper.paper_trade_chart,
    ],
    "kronos": [tools_kronos.kronos_predict],
    "replay": [
        tools_replay.replay_load,
        tools_replay.replay_upload,
        tools_replay.replay_clear,
        tools_replay.replay_chart,
    ],
    "maintenance": [
        tools_maintenance.db_check,
        tools_maintenance.db_update,
    ],
    "deploy": [
        tools_deploy.deploy_status,
        tools_deploy.deploy,
    ],
}

TOOL_GROUP_NAMES: list[str] = list(TOOL_GROUPS)
TOOL_FUNCTIONS: list = [fn for group in TOOL_GROUPS.values() for fn in group]
TOOL_NAMES: list[str] = [fn.__name__ for fn in TOOL_FUNCTIONS]


def _register_tools() -> None:
    for fn in TOOL_FUNCTIONS:
        mcp.add_tool(fn)


_register_tools()


@mcp.resource("trading://markets", name="markets", description="Market profiles (us, ph)")
def markets_resource() -> str:
    return _safe_json(tools_market.get_markets)


@mcp.resource(
    "trading://patterns/catalog",
    name="patterns_catalog",
    description="Pattern catalog (id, name, default version)",
)
def patterns_resource() -> str:
    return _safe_json(tools_patterns.list_patterns)


@mcp.resource(
    "trading://paper/status",
    name="paper_status",
    description="Current paper book status for all markets",
)
def paper_status_resource() -> str:
    return _safe_json(tools_paper.paper_status)


def _safe_json(fn) -> str:
    try:
        return json.dumps(fn(), default=str)
    except Exception as exc:  # noqa: BLE001 - a down web service must not crash the resource
        return json.dumps({"error": str(exc)})


@mcp.prompt(name="daily_review", description="Summarize paper books and latest backtest run")
def daily_review(market: str = "us") -> str:
    try:
        status = json.dumps(tools_paper.paper_status(market=market), default=str)
    except Exception as exc:  # noqa: BLE001
        status = f"(paper status unavailable: {exc})"
    return (
        f"Review the trading bot's paper book for market '{market}'.\n\n"
        f"Current paper status JSON:\n{status}\n\n"
        "Summarize: running state, open positions with R-multiples, closed-trade "
        "win rate/P&L, and any scanner rejections worth noting. Then suggest the "
        "single most useful next action (e.g. a backtest to run), and why."
    )


def run() -> None:
    """Serve MCP over stdio (the agent spawns this process)."""
    mcp.run(transport="stdio")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
