"""Settings tab backing store.

Every field of :class:`config.Settings` is exposed here, grouped for the UI,
with a short description of what it controls. Saving writes the value to the
repo ``.env`` (the same file ``config.py`` loads) and applies it to the running
``settings`` singleton, so most changes take effect immediately.

Deliberate limits:

* Secret values are never returned to the browser. A secret field reports only
  whether it is configured; leaving its input blank keeps the current value,
  and the per-field *Clear* box removes it.
* Web login credentials are written to ``.env`` but not applied live — swapping
  the password/secret mid-request would invalidate the very session doing the
  save. They activate on the next web-service restart.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import types
import typing
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin

from dotenv import dotenv_values
from pydantic import ValidationError

from config import Settings, settings
from utils.logger import log

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

_LOCK = threading.Lock()


class SettingsError(ValueError):
    """A save payload the operator can fix (unknown field, bad value)."""


# ── Field metadata ───────────────────────────────────────────────────────────

SECRET_FIELDS: set[str] = {
    "deepseek_api_key",
    "tv_sessionid",
    "tv_sessionid_sign",
    "anthropic_api_key",
    "telegram_bot_token",
    "db_password",
    "alpaca_api_key",
    "alpaca_secret_key",
    "stocks_history_password",
    "pattern_api_password",
    "web_ui_password",
    "web_ui_users",
    "web_ui_secret_key",
    "mcp_password",
    "mcp_deploy_token",
    "deploy_gateway_token",
    "deploy_tunnel_token",
}

# Written to .env immediately; a restart is required before the running process
# uses them (the bind address is fixed at startup and credentials must not
# change under the session performing the save).
RESTART_FIELDS: set[str] = {
    "web_ui_host",
    "web_ui_port",
    "web_ui_username",
    "web_ui_password",
    "web_ui_users",
    "web_ui_secret_key",
    "web_ui_session_hours",
}

# Never applied to the live ``settings`` object: doing so would invalidate the
# current session (signing key / credentials change mid-request).
BLOCK_LIVE: set[str] = {
    "web_ui_username",
    "web_ui_password",
    "web_ui_users",
    "web_ui_secret_key",
}

# Field names rendered as a checkbox list of the shipped pattern detectors
# instead of a free-text value (see core.pattern_toggles).
CHECKLIST_FIELDS: set[str] = {"disabled_patterns", "disabled_patterns_v2"}

# String fields whose accepted values are a fixed set (the validators enforce
# the same list). Literal fields discover their choices automatically.
CHOICES: dict[str, list[str]] = {
    "market": ["us", "ph"],
    "kronos_gate_mode": ["off", "shadow", "confirm"],
    "kronos_exit_mode": ["off", "shadow", "exit"],
    "kronos_gate_model": ["base", "finetuned"],
    "kronos_device": ["auto", "cuda", "cpu"],
    "alpaca_data_feed": ["sip", "iex"],
    "laya_device": ["cpu", "cuda"],
}

# group id, title, blurb, ordered field names
GROUPS: list[tuple[str, str, str, list[str]]] = [
    (
        "patterns",
        "Patterns",
        "Which detectors may trade. Tick a pattern to scan it; an unticked pattern "
        "is skipped by the scanner, paper books, backtests and the explorer. "
        "Patterns marked Retired or Off by default ship disabled and stay off. "
        "Changes take effect on the next scan or book start.",
        ["disabled_patterns", "disabled_patterns_v2"],
    ),
    (
        "risk",
        "Trading & risk",
        "Master mode, market profile, position caps, stops and the collect-first "
        "ranking path.",
        [
            "trading_mode", "market", "max_daily_loss_usd", "max_daily_loss_php",
            "max_open_positions", "max_open_per_pattern", "min_position_notional",
            "stop_loss_pct", "stop_loss_trail_r", "stop_loss_arm_r",
            "stop_loss_min_risk_frac", "target_max_r", "target_risk_basis", "paper_v2_min_stop_atr", "min_signal_price_usd",
            "short_entries_enabled", "dii_gate_shadow", "paper_dii_gate_mode",
            "collect_first_enabled", "collect_first_top_n",
        ],
    ),
    (
        "paper",
        "Paper trading",
        "Simulated capital, fill costs and the exposure limits of a paper book.",
        [
            "paper_initial_capital", "paper_slippage_pct", "paper_account_model",
            "paper_max_open_per_symbol", "paper_max_gross_exposure",
            "paper_max_volume_participation", "paper_require_liquidity_history",
        ],
    ),
    (
        "stream",
        "Paper trade stream",
        "Replay server that feeds stocks_history bars to paper books while the "
        "cash markets are closed.",
        [
            "papertrade_stream_host", "papertrade_stream_port",
            "papertrade_stream_lookback_bars", "papertrade_stream_interval_seconds",
            "papertrade_stream_start_date", "papertrade_stream_batch_size",
            "papertrade_stream_preload_workers",
        ],
    ),
    (
        "scanner",
        "Scanner & TradingView",
        "What the scanner watches, how often it runs, its worker pool and the "
        "TradingView request pacing.",
        [
            "watchlist", "tv_screener", "tv_exchange", "tv_exchange_overrides",
            "tv_use_ta_fallback", "tv_history_days", "scan_interval_seconds",
            "scanner_concurrency", "scanner_analyze_workers",
            "tv_screener_min_interval_seconds", "tv_screener_max_retries",
            "tv_screener_retry_backoff_seconds", "tv_sessionid",
            "tv_sessionid_sign", "tv_indicator_on_indicator_limit",
            "tv_chart_connections",
        ],
    ),
    (
        "volume",
        "Volume confirmation gate",
        "Relative-volume and OBV filter applied after a chart pattern fires.",
        ["volume_gate_enabled", "volume_gate_rvol_min", "volume_gate_obv_bars"],
    ),
    (
        "kronos",
        "Kronos model",
        "Entry confirmation, open-position protection and GPU worker settings "
        "for the Kronos forecast model.",
        [
            "kronos_batch_size", "kronos_min_move_pct", "kronos_regime_enabled",
            "kronos_regime_days", "kronos_gate_mode", "kronos_gate_samples",
            "kronos_gate_model", "kronos_require_provenance", "kronos_device",
            "kronos_gate_sideways_atr", "kronos_gate_sideways_min_pct",
            "kronos_gate_sideways_max_pct", "kronos_gate_min_agree",
            "kronos_gate_cache_minutes", "kronos_gate_timeout_s",
            "kronos_gate_max_checks_per_scan", "kronos_exit_mode",
            "kronos_exit_min_agree", "kronos_exit_min_adverse_atr",
        ],
    ),
    (
        "vision",
        "Vision confirmation",
        "Optional Claude-vision check of the rendered chart before an entry.",
        [
            "anthropic_api_key", "vision_confirmation_enabled",
            "vision_min_indicator_confidence",
        ],
    ),
    (
        "notifications",
        "Notifications",
        "Telegram alerts sent by the bot.",
        ["telegram_bot_token", "telegram_chat_id"],
    ),
    (
        "ibkr",
        "IBKR connection",
        "Interactive Brokers endpoint used for live orders.",
        ["ibkr_host", "ibkr_port", "ibkr_client_id"],
    ),
    (
        "database",
        "PostgreSQL",
        "Stock-history database. Discrete db_* fallbacks are used only when "
        "DATABASE_URL is empty.",
        [
            "database_url", "db_host", "db_port", "db_name", "db_user",
            "db_password",
        ],
    ),
    (
        "alpaca",
        "Alpaca & hourly data",
        "Hourly OHLCV bulk fetch and the remote stocks_history API reads.",
        [
            "alpaca_api_key", "alpaca_secret_key", "alpaca_paper",
            "alpaca_data_feed", "alpaca_requests_per_min",
            "hourly_fetch_batch_size", "hourly_fetch_workers",
            "stocks_history_url", "stocks_history_username",
            "stocks_history_password", "stocks_history_owner",
        ],
    ),
    (
        "registry",
        "Remote pattern registry",
        "Clients without a local editor database read pattern versions from "
        "this API instead.",
        [
            "pattern_api_url", "pattern_api_username", "pattern_api_password",
            "pattern_api_owner",
        ],
    ),
    (
        "pattern_edit",
        "Pattern editing & sandbox",
        "Sandbox used to execute approved pattern versions and the DeepSeek "
        "model that proposes edits.",
        [
            "pattern_editor_database_url", "pattern_editor_schema",
            "pattern_edit_cgroup_root", "pattern_edit_worker_python",
            "backtest_dataset_dir", "deepseek_api_key", "deepseek_base_url",
            "deepseek_model", "deepseek_timeout_seconds", "deepseek_max_retries",
            "deepseek_max_concurrency",
        ],
    ),
    (
        "web",
        "Web UI & authentication",
        "Bind address, login credentials and the optional Tasks / Builder "
        "schemas.",
        [
            "web_ui_host", "web_ui_port", "web_ui_username", "web_ui_password",
            "web_ui_users", "web_ui_secret_key", "web_ui_https",
            "web_ui_session_hours", "tasks_enabled", "task_database_url",
            "task_schema", "rules_database_url", "rules_schema",
            "hermes_dashboard_url", "deepseek_harness_url",
            "deepseek_harness_token_file",
        ],
    ),
    (
        "laya",
        "Laya assessment",
        "Optional local text-assessment worker.",
        [
            "laya_enabled", "laya_worker_python", "laya_model_path",
            "laya_device", "laya_max_pending", "laya_timeout_seconds",
            "laya_store_path",
        ],
    ),
    (
        "mcp",
        "MCP server",
        "Local stdio MCP server that lets an AI agent operate the bot through "
        "the web API.",
        [
            "mcp_api_url", "mcp_username", "mcp_password", "mcp_timeout_seconds",
            "mcp_max_history_bars", "mcp_allow_destructive",
            "mcp_allow_maintenance", "mcp_allow_deploy", "mcp_deploy_url",
            "mcp_deploy_token",
        ],
    ),
    (
        "deploy",
        "Deploy gateway",
        "Authenticated webhook that pulls a branch and restarts the web service "
        "on this host.",
        [
            "deploy_gateway_host", "deploy_gateway_port", "deploy_gateway_token",
            "deploy_repo_dir", "deploy_allowed_refs", "deploy_restart_units",
            "deploy_allow_dirty", "deploy_tunnel_token", "deploy_tunnel_hostname",
        ],
    ),
]

# field -> (label, description). Labels are title-cased from the env name when
# omitted; every field needs a description so the tab is self-explanatory.
INFO: dict[str, tuple[str, str]] = {
    # Patterns
    "disabled_patterns": ("v1 patterns", "Tick a pattern to scan it. Unticked patterns are skipped by the scanner, paper books, backtests and the explorer. Boxes marked Retired or Off by default ship disabled and cannot be ticked."),
    "disabled_patterns_v2": ("v2 patterns", "Tick a patterns_v2 detector to scan it. Unticked detectors are skipped by patterns_v2 scans, replays and paper books. Boxes marked Off by default ship disabled and cannot be ticked."),
    # Trading & risk
    "trading_mode": ("Trading mode", "Master mode: paper simulates fills; live sends orders to IBKR. Change deliberately."),
    "market": ("Default market", "us = NASDAQ/NYSE USD book; ph = PSE PHP long-only daytime book. Individual runs can override."),
    "max_daily_loss_usd": ("Max daily loss (USD)", "Stop opening US trades for the day once the loss reaches this USD amount."),
    "max_daily_loss_php": ("Max daily loss (PHP)", "Stop opening PH trades for the day once the loss reaches this PHP amount."),
    "max_open_positions": ("Max open positions", "Concurrent open positions allowed in a book. 0 = unlimited."),
    "max_open_per_pattern": ("Max open per pattern", "Concurrent seats allowed in any one pattern, so the book cannot collapse into a single setup. 0 = unlimited."),
    "min_position_notional": ("Min position notional", "Skip leftover fills below this notional after risk/exposure caps (e.g. a 1-share crumb)."),
    "stop_loss_pct": ("Legacy / indicator fixed stop", "Stop distance from signal price (0.03 = 3%). Patternsv2 uses its own structural/ATR stop. This does not change the separate 3% net paper budget."),
    "stop_loss_trail_r": ("Legacy / indicator trail distance (R)", "Fixed-stop trail giveback. Does not configure the Patternsv2 ladder or the independent paper trail."),
    "stop_loss_arm_r": ("Legacy / indicator trail activation (R)", "Fixed-stop trail activation. Does not configure the Patternsv2 ladder or the independent paper trail."),
    "stop_loss_min_risk_frac": ("Min gap risk fraction", "Cancel a next-open fill when an adverse gap leaves less than this fraction of the planned stop distance. 0 disables."),
    "target_max_r": ("Max target (R)", "Cap a measured target using the selected gross risk basis. 0 preserves the measured target."),
    "target_risk_basis": ("Target risk basis", "Pattern uses the structural/ATR stop. Paper initial uses the nearer initial paper/structural stop after configured costs. Both measure gross price distance; the 3% paper budget is net of costs."),
    "paper_v2_min_stop_atr": ("V2 minimum initial stop (ATR)", "Optional paper admission minimum using saved signal-time ATR and actual initial gross protection. 0 records evidence only. Keep thresholds fixed before evaluation; this does not widen the 3% net budget."),
    "min_signal_price_usd": ("Min US signal price", "Reject US pattern entries below this price. 0 disables the floor."),
    "short_entries_enabled": ("Short entries", "Allow US paper short entries through the normal causal entry/exit path. PH remains long-only."),
    "dii_gate_shadow": ("DII gate shadow mode", "Record DII band rejections without blocking. The required three-day daily bulls uptrend still blocks BUY entries."),
    "paper_dii_gate_mode": ("Paper DII gate mode", "shadow records band votes; filter enforces bands. Every BUY requires the three-day daily bulls uptrend in both modes."),
    "collect_first_enabled": ("Collect first", "Collect signals without opening, rank by reward:risk, and open only the top N. Expensive gates run only on winners."),
    "collect_first_top_n": ("Collect first top N", "Number of ranked signals to open when collect-first is enabled."),
    # Paper trading
    "paper_initial_capital": ("Initial capital", "Starting cash for a new paper book, in the book's market currency."),
    "paper_slippage_pct": ("Slippage", "Fraction of price paid as simulated slippage on every paper fill (0.0005 = 5 bps)."),
    "paper_account_model": ("Account model", "funded = one shared cash account; independent-fixed-notional = each position sized independently."),
    "paper_max_open_per_symbol": ("Max open per symbol", "Concurrent positions allowed in one symbol. 0 = unlimited."),
    "paper_max_gross_exposure": ("Max gross exposure", "Gross exposure as a multiple of equity (1.0 = fully invested, no leverage)."),
    "paper_max_volume_participation": ("Max volume participation", "A fill may use at most this fraction of the bar's volume."),
    "paper_require_liquidity_history": ("Require liquidity history", "Reject symbols without enough volume history to estimate participation."),
    # Stream
    "papertrade_stream_host": ("Stream host", "Interface the paper-stream replay server binds to."),
    "papertrade_stream_port": ("Stream port", "Port the paper-stream replay server listens on."),
    "papertrade_stream_lookback_bars": ("Lookback bars", "History bars sent per symbol on the first stream fill; later scans send only the new candle."),
    "papertrade_stream_interval_seconds": ("Stream interval", "0 = start the next simulated day as soon as the scan finishes; >0 sleeps the remainder."),
    "papertrade_stream_start_date": ("Start date", "YYYY-MM-DD cursor for replay. Empty starts near the end of each tape; the UI datepicker overrides it."),
    "papertrade_stream_batch_size": ("Batch size", "Symbols requested per WebSocket round-trip (1-200)."),
    "papertrade_stream_preload_workers": ("Preload workers", "Parallel history fetches while preloading the stream universe (1-32)."),
    # Scanner
    "watchlist": ("Watchlist", "Comma-separated symbols scanned when no universe is supplied."),
    "tv_screener": ("TradingView screener", "Screener market: america, crypto, forex or cfd."),
    "tv_exchange": ("Default exchange", "Exchange hint for the TradingView screener and MCP (e.g. NASDAQ, NYSE)."),
    "tv_exchange_overrides": ("Exchange overrides", "Comma-separated SYMBOL:EXCHANGE pairs, e.g. SPY:AMEX, BDO:PSE."),
    "tv_use_ta_fallback": ("TA fallback", "Unused legacy flag kept for .env compatibility. Leave off."),
    "tv_history_days": ("History days", "Daily bars pulled per symbol (clamped to 30-512). Planned lookback for patterns and Kronos."),
    "scan_interval_seconds": ("Scan interval (s)", "Seconds between scan-loop passes. Daily/weekly patterns only need hourly polling."),
    "scanner_concurrency": ("Scanner concurrency", "Symbols processed concurrently; each worker opens its own MCP session."),
    "scanner_analyze_workers": ("Analyze workers", "Processes for pattern.analyze(). 0 = auto (2-8 from CPU count); 1 = inline on the scan loop."),
    "tv_screener_min_interval_seconds": ("Screener min interval (s)", "Minimum gap between TradingView scanner POSTs across all workers. 0.5s is about 2 req/s."),
    "tv_screener_max_retries": ("Screener retries", "Retries after an HTTP 429 from the TradingView screener."),
    "tv_screener_retry_backoff_seconds": ("Screener backoff (s)", "Base backoff for screener retries; doubles on each attempt."),
    "tv_sessionid": ("TradingView session id", "Logged-in TradingView cookie value (not the password). Empty disables indicator-on-indicator studies."),
    "tv_sessionid_sign": ("TradingView session signature", "Companion cookie for the TradingView session id."),
    "tv_indicator_on_indicator_limit": ("Study limit", "Max indicator-on-indicator studies allowed by the TradingView plan (Plus 9, Ultimate 49)."),
    "tv_chart_connections": ("Chart connections", "Max concurrent TradingView chart connections allowed by the plan."),
    # Volume
    "volume_gate_enabled": ("Enable volume gate", "Require relative volume and OBV direction to agree with a chart-pattern signal."),
    "volume_gate_rvol_min": ("Min relative volume", "Signal-bar volume divided by SMA20 volume must reach this value."),
    "volume_gate_obv_bars": ("OBV slope bars", "Bars used to measure the on-balance-volume slope."),
    # Kronos
    "kronos_batch_size": ("Batch size", "Series per predict_batch call on CUDA; capped at 4 when CUDA is unavailable."),
    "kronos_min_move_pct": ("Min forecast move", "Minimum absolute 3-day close-to-close forecast used by the evaluation slice metrics."),
    "kronos_regime_enabled": ("Regime gate", "Enable the shorts-only tape-regime fade. Requires the segment to be wired into a run path."),
    "kronos_regime_days": ("Regime lookback days", "Trading days in the universe mean-return lookback that defines tape strength."),
    "kronos_gate_mode": ("Entry gate mode", "confirm = require up for a long or down for a short; flat blocks entries. Uses direction only. shadow = record without blocking; off = skip."),
    "kronos_gate_samples": ("Entry samples", "Independent one-day simulations per symbol (odd, 1-9)."),
    "kronos_gate_model": ("Checkpoint", "Which Kronos checkpoint the GPU worker loads: base or finetuned."),
    "kronos_require_provenance": ("Require provenance", "Reject an entry when the forecast worker cannot prove which checkpoint produced the vote."),
    "kronos_device": ("Worker device", "auto = CUDA when it has headroom else CPU; cuda = require the GPU; cpu = always CPU."),
    "kronos_gate_sideways_atr": ("Sideways band (ATR)", "Dead-zone width as a multiple of ATR(14) percent before min/max clamps."),
    "kronos_gate_sideways_min_pct": ("Sideways min %", "Lower clamp for the sideways dead zone, in percent."),
    "kronos_gate_sideways_max_pct": ("Sideways max %", "Upper clamp for the sideways dead zone, in percent."),
    "kronos_gate_min_agree": ("Votes to confirm", "One-day forecasts that must agree to confirm or oppose an entry."),
    "kronos_gate_cache_minutes": ("Vote cache (min)", "Lifetime of the per-symbol/anchor/model vote cache."),
    "kronos_gate_timeout_s": ("Worker timeout (s)", "Deadline for one batched Kronos worker call."),
    "kronos_gate_max_checks_per_scan": ("Max checks per scan", "Hard cap on forecast contexts per flush; open positions have priority."),
    "kronos_exit_mode": ("Exit mode", "exit = queue a next-open exit on an opposing direction regardless of profit or distance from entry; flat holds. shadow = record only; off = skip."),
    "kronos_exit_min_agree": ("Exit votes", "One-day forecasts that must oppose an open position to queue an exit."),
    "kronos_exit_min_adverse_atr": ("Legacy exit threshold (unused)", "Ignored. Paper Kronos acts only on direction; distance from entry does not delay an opposing exit."),
    # Vision
    "anthropic_api_key": ("Anthropic API key", "Claude vision key used to confirm rendered pattern charts. Server-side only."),
    "vision_confirmation_enabled": ("Enable vision check", "Send a rendered chart to Claude and require CONFIRM before an entry."),
    "vision_min_indicator_confidence": ("Min indicator confidence", "Indicator confidence (0-1) required before the vision check is even attempted."),
    # Notifications
    "telegram_bot_token": ("Telegram bot token", "Bot token used for notifications. Empty disables Telegram."),
    "telegram_chat_id": ("Telegram chat id", "Destination chat for bot notifications."),
    # IBKR
    "ibkr_host": ("IBKR host", "Host of TWS or IB Gateway."),
    "ibkr_port": ("IBKR port", "7497 = paper, 7496 = live."),
    "ibkr_client_id": ("IBKR client id", "Client id used for the TWS/Gateway API session."),
    # PostgreSQL
    "database_url": ("Database URL", "PostgreSQL DSN for stock history and the shared stores. Empty falls back to the db_* fields."),
    "db_host": ("DB host", "Host or socket directory used when DATABASE_URL is empty."),
    "db_port": ("DB port", "PostgreSQL port for the discrete fallback connection."),
    "db_name": ("DB name", "Database name for the discrete fallback connection."),
    "db_user": ("DB user", "Role for the discrete fallback connection."),
    "db_password": ("DB password", "Password for the discrete fallback connection. Empty uses local socket peer auth."),
    # Alpaca
    "alpaca_api_key": ("Alpaca API key", "Key for the hourly OHLCV bulk fetch and the asset list."),
    "alpaca_secret_key": ("Alpaca secret key", "Secret paired with the Alpaca API key."),
    "alpaca_paper": ("Alpaca paper keys", "Load the asset list from the paper host when true."),
    "alpaca_data_feed": ("Alpaca data feed", "sip = consolidated tape; iex = that exchange only (thinner volume)."),
    "alpaca_requests_per_min": ("Alpaca requests/min", "Global request budget kept under the plan's 200/min limit."),
    "hourly_fetch_batch_size": ("Hourly batch size", "Symbols per Alpaca bars request (1-200)."),
    "hourly_fetch_workers": ("Hourly workers", "Concurrent batch workers, each with its own connection and staging table."),
    "stocks_history_url": ("stocks_history URL", "Remote OHLCV API used by local --web. Empty auto-selects the project default; the VPS owner leaves it empty."),
    "stocks_history_username": ("stocks_history user", "Basic-auth user for the remote history API. Empty falls back to WEB_UI_USERNAME."),
    "stocks_history_password": ("stocks_history password", "Basic-auth password for the remote history API."),
    "stocks_history_owner": ("stocks_history owner", "Set true on the host that owns the database so it writes Postgres and serves /api/history."),
    # Pattern registry
    "pattern_api_url": ("Pattern API URL", "Remote pattern registry. Empty uses the local editor database."),
    "pattern_api_username": ("Pattern API user", "Basic-auth user for the remote pattern registry."),
    "pattern_api_password": ("Pattern API password", "Must be the serving host's real WEB_UI_PASSWORD (admin:admin is refused)."),
    "pattern_api_owner": ("Pattern API owner", "Set true on the host that serves the registry so it never proxies to itself."),
    # Pattern editing
    "pattern_editor_database_url": ("Editor database URL", "Sandbox PostgreSQL DSN for approved pattern versions. Empty reuses DATABASE_URL/db_*."),
    "pattern_editor_schema": ("Editor schema", "Schema holding approved pattern versions (production: pattern_editor)."),
    "pattern_edit_cgroup_root": ("Sandbox cgroup root", "Delegated cgroup v2 subtree for executing approved versions. Empty disables execution."),
    "pattern_edit_worker_python": ("Sandbox interpreter", "Isolated Python interpreter used to run approved pattern versions. Empty falls back to shipped code."),
    "backtest_dataset_dir": ("Backtest dataset dir", "Frozen daily-bar cache used by shared backtest runs. Missing symbols are fetched and cached here."),
    "deepseek_api_key": ("DeepSeek API key", "Server-side key for AI pattern edits. Never logged or returned to a client."),
    "deepseek_base_url": ("DeepSeek base URL", "Endpoint for the DeepSeek API used by the pattern editor."),
    "deepseek_model": ("DeepSeek model", "API model id (verified: deepseek-flash). Change only after checking the docs."),
    "deepseek_timeout_seconds": ("DeepSeek timeout (s)", "Deadline for one DeepSeek completion."),
    "deepseek_max_retries": ("DeepSeek retries", "Retries after a transient DeepSeek API failure."),
    "deepseek_max_concurrency": ("DeepSeek concurrency", "Concurrent DeepSeek requests across pattern-edit jobs."),
    # Web
    "web_ui_host": ("Bind host", "Interface the web server binds to. Requires a restart."),
    "web_ui_port": ("Bind port", "Port the web server listens on. Requires a restart."),
    "web_ui_username": ("Primary username", "Primary login and the identity used for derived credentials. Requires a restart."),
    "web_ui_password": ("Primary password", "Required; the server refuses to start when empty. Written now, activated on restart."),
    "web_ui_users": ("Extra logins", "Comma-separated user:password logins added to the primary pair. Written now, activated on restart."),
    "web_ui_secret_key": ("Cookie signing key", "Signing key for session cookies. Empty derives one from the credentials. Restart required."),
    "web_ui_https": ("Force Secure cookie", "Set true when serving over HTTPS so the session cookie gets the Secure flag."),
    "web_ui_session_hours": ("Session lifetime (h)", "Idle session lifetime in hours. Requires a restart."),
    "tasks_enabled": ("Tasks tab", "Show the PostgreSQL-backed Kanban board in the navbar."),
    "task_database_url": ("Tasks database URL", "PostgreSQL DSN for the Tasks board. Empty reuses DATABASE_URL/db_*."),
    "task_schema": ("Tasks schema", "Schema holding the Tasks board (production: tasks)."),
    "rules_database_url": ("Rules database URL", "PostgreSQL DSN for the Builder's indicator rules. Empty reuses DATABASE_URL/db_*."),
    "rules_schema": ("Rules schema", "Schema holding Builder rules (production: rules)."),
    "hermes_dashboard_url": ("Hermes dashboard URL", "Loopback URL of the Hermes Agent dashboard; set to proxy it at /hermes and show the tab. Empty hides it."),
    "deepseek_harness_url": ("DeepSeek Harness URL", "Loopback URL of the DeepSeek Harness (dsh) Web UI, e.g. http://127.0.0.1:3080; set to serve it at /deepseek (by URL, no navbar entry). Empty disables it."),
    "deepseek_harness_token_file": ("DeepSeek launch token file", "File run_dsh.sh writes the harness's launch token to, so the /deepseek tab signs in without the printed tokenized URL. Empty disables automatic sign-in."),
    # Laya
    "laya_enabled": ("Enable Laya", "Allow the local text-assessment worker to run. Off by default."),
    "laya_worker_python": ("Worker interpreter", "Python path of the isolated Laya environment. Empty renders the tab unavailable."),
    "laya_model_path": ("Model path", "Pinned local checkpoint directory. Empty means no model is loaded."),
    "laya_device": ("Worker device", "Device used by the Laya worker (cpu is the validated default)."),
    "laya_max_pending": ("Max pending", "Bound on queued Laya assessments."),
    "laya_timeout_seconds": ("Timeout (s)", "Deadline for one Laya assessment."),
    "laya_store_path": ("Store path", "SQLite file for the local Laya research workspace."),
    # MCP
    "mcp_api_url": ("Web API URL", "Base URL of the running web UI that MCP tools call."),
    "mcp_username": ("MCP username", "Basic-auth user for /api/*. Empty falls back to WEB_UI_USERNAME."),
    "mcp_password": ("MCP password", "Basic-auth password for /api/*. Empty falls back to WEB_UI_PASSWORD."),
    "mcp_timeout_seconds": ("MCP timeout (s)", "HTTP timeout for MCP tool calls."),
    "mcp_max_history_bars": ("Max history bars", "Hard cap on bars returned by get_history_bars (the API allows 2000)."),
    "mcp_allow_destructive": ("Allow destructive tools", "Enable paper reset/reset-logs, pattern default/archive and replay clear/upload tools."),
    "mcp_allow_maintenance": ("Allow maintenance tools", "Enable db_check and db_update tools."),
    "mcp_allow_deploy": ("Allow deploy tools", "Enable deploy_status and deploy tools."),
    "mcp_deploy_url": ("Deploy gateway URL", "Loopback URL targeted by the deploy_* MCP tools."),
    "mcp_deploy_token": ("Deploy gateway token", "Bearer token sent to the deploy gateway."),
    # Deploy
    "deploy_gateway_host": ("Gateway host", "Interface the deploy webhook binds to (loopback only by default)."),
    "deploy_gateway_port": ("Gateway port", "Port the deploy webhook listens on."),
    "deploy_gateway_token": ("Gateway token", "Required bearer token; the gateway refuses to bind without one."),
    "deploy_repo_dir": ("Deploy repo dir", "Repository the gateway checks out. Empty means this repo."),
    "deploy_allowed_refs": ("Allowed refs", "Comma-separated branch allowlist. Empty allows any branch on origin."),
    "deploy_restart_units": ("Restart units", "Comma-separated systemd --user units restarted after a successful checkout."),
    "deploy_allow_dirty": ("Allow dirty tree", "Allow deploying over uncommitted changes. Off by default."),
    "deploy_tunnel_token": ("Deploy tunnel token", "Cloudflare Tunnel token for the deploy gateway. Empty skips the tunnel."),
    "deploy_tunnel_hostname": ("Deploy tunnel hostname", "Public hostname served by the deploy gateway tunnel."),
}


# Active .env keys that are not Settings fields: read by shell launchers or
# kept only for compatibility. They are still editable here so the tab covers
# every variable in the env file.
EXTRA_INFO: dict[str, tuple[str, str]] = {
    "cloudflare_tunnel_hostname": ("Cloudflare tunnel hostname", "Public hostname for run_web.sh's Cloudflare Tunnel. Read by the shell launcher, not the Python app."),
    "cloudflare_tunnel_name": ("Cloudflare tunnel name", "Tunnel name used by the run_web.sh setup commands."),
    "cloudflare_tunnel_token": ("Cloudflare tunnel token", "Cloudflare Tunnel token for the public web UI. Read by run_web.sh."),
    "deploy_tunnel_name": ("Deploy tunnel name", "Tunnel name used by the deploy-gateway setup script."),
    "kronos_gate_enabled": ("Legacy Kronos gate", "Legacy switch ignored by config.py; use KRONOS_GATE_MODE instead."),
    "kronos_gate_adjust_exits": ("Legacy Kronos exits", "Legacy switch ignored by config.py; use KRONOS_EXIT_MODE instead."),
    "kronos_sample_count": ("Legacy Kronos samples", "Legacy value ignored by config.py; use KRONOS_GATE_SAMPLES instead."),
    "kronos_use_finetuned": ("Legacy Kronos checkpoint", "Legacy switch ignored by config.py; use KRONOS_GATE_MODEL instead."),
    "polygon_api_key": ("Polygon API key (unused)", "Unused: Polygon data was superseded by Alpaca."),
}

EXTRA_KIND: dict[str, str] = {
    "kronos_gate_enabled": "bool",
    "kronos_gate_adjust_exits": "bool",
    "kronos_use_finetuned": "bool",
    "kronos_sample_count": "int",
}

EXTRA_SECRETS: set[str] = {"cloudflare_tunnel_token", "polygon_api_key"}

EXTRA_GROUP = (
    "extras",
    "Host & legacy variables",
    "Variables present in the env file that are not read by config.py — host "
    "launchers and legacy compatibility keys. Saving updates the file; restart "
    "the relevant service or script to apply them.",
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def _kind(name: str, annotation: Any) -> tuple[str, list[str]]:
    if name in SECRET_FIELDS:
        return "secret", []
    if name in CHECKLIST_FIELDS:
        return "checklist", []
    base = _unwrap_optional(annotation)
    if base is bool:
        return "bool", []
    if base is int:
        return "int", []
    if base is float:
        return "float", []
    if get_origin(base) is typing.Literal:
        return "select", [str(v) for v in get_args(base)]
    if isinstance(base, type) and issubclass(base, Enum):
        return "select", [str(member.value) for member in base]
    if name in CHOICES:
        return "select", CHOICES[name]
    return "text", []


def _label(name: str) -> str:
    if name in INFO:
        return INFO[name][0]
    return name.replace("_", " ").title()


def _default_for(name: str) -> Any:
    field = Settings.model_fields.get(name)
    if field is None or field.is_required():
        return None
    factory = getattr(field, "default_factory", None)
    if factory is not None:
        try:
            return factory()
        except Exception:
            return None
    return field.default


def _display_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _bind_checklist(entry: dict[str, Any], name: str) -> None:
    """Turn a pattern-toggle field into its checkbox list.

    ``value`` becomes the ids currently muted (so the template can untick them)
    and ``forced`` the ids that ship disabled. Neither depends on the editor
    database; a detector package that cannot load leaves the list empty rather
    than breaking the page.
    """
    from core import pattern_toggles

    try:
        options = pattern_toggles.catalog(name)
        muted = pattern_toggles.muted(name)
    except Exception:  # noqa: BLE001 - a degraded page beats a 500
        log.exception(f"Settings | pattern toggle catalog unavailable for {name}")
        options, muted = [], []
    entry["options"] = options
    entry["forced"] = [option["id"] for option in options if option["forced"]]
    entry["value"] = muted
    entry["default"] = []


def _all_fields() -> list[str]:
    return list(Settings.model_fields)


def _catalog_fields() -> dict[str, dict[str, Any]]:
    """Everything the UI needs, keyed by setting name."""
    fields: dict[str, dict[str, Any]] = {}
    for name in _all_fields():
        annotation = Settings.model_fields[name].annotation
        kind, options = _kind(name, annotation)
        raw = getattr(settings, name, _default_for(name))
        entry: dict[str, Any] = {
            "name": name,
            "env": name.upper(),
            "label": _label(name),
            "description": INFO.get(name, ("", ""))[1] or "No description yet.",
            "kind": kind,
            "options": options,
            "restart": name in RESTART_FIELDS,
            "default": _display_value(_default_for(name)),
        }
        if kind == "secret":
            # Never echo the secret itself.
            entry["value"] = ""
            entry["configured"] = bool(str(raw or "").strip())
        elif kind == "checklist":
            # value = the ids currently muted; options = every shipped detector.
            _bind_checklist(entry, name)
        else:
            entry["value"] = _display_value(raw)
        fields[name] = entry
    return fields


def _extra_values(path: Path | None = None) -> dict[str, str]:
    """Active env-file keys that are not Settings fields, lowercased."""
    source = path or ENV_PATH
    if not source.exists():
        return {}
    known = set(Settings.model_fields)
    values = dotenv_values(source)
    return {
        key.lower(): ("" if value is None else value)
        for key, value in values.items()
        if key.lower() not in known
    }


def _extra_field(name: str, raw: str) -> dict[str, Any]:
    if name in EXTRA_KIND:
        kind = EXTRA_KIND[name]
    elif raw.strip().lower() in ("true", "false"):
        kind = "bool"
    elif re.fullmatch(r"-?\d+", raw.strip()):
        kind = "int"
    else:
        kind = "text"
    if name in EXTRA_SECRETS:
        kind = "secret"
    label, description = EXTRA_INFO.get(
        name, (name.replace("_", " ").title(), "Extra env-file variable.")
    )
    entry: dict[str, Any] = {
        "name": name,
        "env": name.upper(),
        "label": label,
        "description": description,
        "kind": kind,
        "options": [],
        "restart": True,
        "default": None,
    }
    if kind == "secret":
        entry["value"] = ""
        entry["configured"] = bool(raw.strip())
    elif kind == "bool":
        entry["value"] = raw.strip().lower() == "true"
    elif kind == "int":
        try:
            entry["value"] = int(raw)
        except ValueError:
            entry["value"] = raw
    else:
        entry["value"] = raw
    return entry


def describe() -> dict[str, Any]:
    """Return the grouped catalog with values safe to send to the browser."""
    fields = _catalog_fields()
    groups = []
    for gid, title, blurb, names in GROUPS:
        groups.append({
            "id": gid,
            "title": title,
            "blurb": blurb,
            "fields": [fields[name] for name in names if name in fields],
        })
    extras = [_extra_field(name, raw) for name, raw in _extra_values().items()]
    if extras:
        gid, title, blurb = EXTRA_GROUP
        groups.append({"id": gid, "title": title, "blurb": blurb, "fields": extras})
    return {
        "groups": groups,
        "env_file": str(ENV_PATH),
        "count": len(fields) + len(extras),
    }


# ── .env persistence ─────────────────────────────────────────────────────────

_ENV_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return ""
    text = str(value)
    # Quote anything a dotenv parser could split on: whitespace, comments and
    # quotes. Keeps passwords / comma lists / paths safe.
    if text == "" or re.search(r"[\s#'\"]", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _write_env(changes: dict[str, str], path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(changes)
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Updated from the Settings tab")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    text = "\n".join(out).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# ── Save ─────────────────────────────────────────────────────────────────────

def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        parts.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(parts)


def save(values: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist and live-apply a settings payload.

    Unknown names and values rejected by the model's validators raise
    :class:`SettingsError`. Secret fields absent from the payload keep their
    current value; an explicit empty string clears one.
    """
    if not isinstance(values, dict):
        raise SettingsError("values must be an object")

    known = set(_all_fields())
    extras = _extra_values()
    unknown = sorted(set(values) - known - set(extras))
    if unknown:
        raise SettingsError("Unknown setting(s): " + ", ".join(unknown))

    current = settings.model_dump()
    merged = dict(current)
    merged.update({name: value for name, value in values.items() if name in known})

    if "web_ui_password" in values and not str(values["web_ui_password"] or "").strip():
        raise SettingsError("web_ui_password cannot be empty: the web server refuses to start without it")

    try:
        validated = Settings(**merged)
    except ValidationError as exc:
        raise SettingsError(_validation_message(exc)) from exc

    changes: dict[str, str] = {}
    pending_apply: dict[str, Any] = {}
    applied: list[str] = []
    restart_required: list[str] = []
    for name, raw in values.items():
        if name not in known:
            # Env-file-only variable (host launcher / legacy key): persist, but
            # there is nothing in the Python process to apply it to.
            if _serialize(raw) != _serialize(extras.get(name, "")):
                changes[name.upper()] = _serialize(raw)
            restart_required.append(name)
            continue
        new_value = getattr(validated, name)
        old_value = getattr(settings, name, None)
        if new_value != old_value:
            changes[name.upper()] = _serialize(new_value)
            if name in RESTART_FIELDS:
                restart_required.append(name)
        if name not in BLOCK_LIVE:
            pending_apply[name] = new_value
        elif name not in restart_required:
            restart_required.append(name)

    # Persist first: never leave the running process using a value the env file
    # does not contain if the write fails.
    if changes:
        with _LOCK:
            _write_env(changes, ENV_PATH)
        log.info(f"Settings | saved {len(changes)} value(s) to {ENV_PATH.name}: {', '.join(changes)}")

    for name, new_value in pending_apply.items():
        try:
            setattr(settings, name, new_value)
            applied.append(name)
        except Exception:
            log.exception(f"Settings | could not apply {name} live")
            restart_required.append(name)

    # The v2 toggles have to reach the loaded detector package, which reads its
    # DISABLED set at call time instead of taking it as a scan argument.
    try:
        from core import pattern_toggles

        pattern_toggles.apply()
    except Exception:  # noqa: BLE001 - a broken detector package must not fail the save
        log.exception("Settings | could not apply the pattern toggles")

    fields = _catalog_fields()
    extra_after = {
        name: _extra_field(name, raw) for name, raw in _extra_values().items()
    }
    return {
        "saved": sorted({name for name in values}),
        "applied": sorted(set(applied)),
        "restart_required": sorted(set(restart_required)),
        "values": {
            name: entry["value"]
            for name, entry in {**fields, **extra_after}.items()
            if entry["kind"] != "secret"
        },
        "configured": {
            name: entry["configured"]
            for name, entry in {**fields, **extra_after}.items()
            if entry["kind"] == "secret"
        },
        "env_file": str(ENV_PATH),
    }
