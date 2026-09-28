"""Shared backtest parameter definitions and request validation.

UI-independent: web, desktop and automatic editor jobs all build their typed
requests from here, so headless web startup never imports Tkinter. The legacy
offline form definitions (``PARAMS``) live here unchanged; the historical-stream
definitions (``REPLAY_PARAMS``) extend them for the shared service.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional

from core.engine_defaults import ENGINE
from core.market import default_market, get_market
from core.pattern_editor_contracts import (
    BacktestSettings, ExecutionSettings, Parameter, ReplayWindow,
)

# Each entry: (key, label, description, type, default, choices_or_None)
#   type: "entry" (free text), "spin" (numeric spinbox), "combo" (dropdown),
#         "check" (checkbox)
#   For "spin": default is packed as (default_value, min, max, increment).
PARAMS: list[tuple[str, str, str, str, Any, Optional[list[str]]]] = [
    (
        "market", "Market",
        "US = NASDAQ/NYSE, USD, shorts allowed. PH = PSE, PHP, long-only.",
        "combo", default_market().id, ["us", "ph"],
    ),
    (
        "pattern_filter", "Pattern filter",
        "Filter to one pattern (case-insensitive substring). Blank = all patterns.",
        "combo", "", None,
    ),
    (
        "universe", "Universe",
        "Ticker list under data/universes/. Blank = the pattern's own .cjs "
        "universe when a Pattern filter is set, else 'default'.",
        "entry", "", None,
    ),
    (
        "barcache_dir", "Barcache dir",
        "Offline daily-bar cache (build with scripts/build_barcache.py).",
        "entry", "data/barcache", None,
    ),
    (
        "extra_symbols", "Additional symbols",
        "Optional extra tickers (comma or space separated).",
        "entry", "", None,
    ),
    (
        "txn_cost_pct", "Txn cost (per leg)",
        "0.0 = documented headline numbers; 0.001 matches the .cjs 'cost optional' mode.",
        "spin", (0.0, 0.0, 0.01, 0.0001), None,
    ),
    (
        "max_workers", "CPU workers",
        f"Detected {os.cpu_count() or '?'} cores. 0 = use all.",
        "spin", (max(1, (os.cpu_count() or 2) - 1), 0, 64, 1), None,
    ),
]

# Historical-stream / shared-service definitions. Offline runs ignore them.
REPLAY_PARAMS: list[tuple[str, str, str, str, Any, Optional[list[str]]]] = [
    (
        "mode", "Backtest mode",
        "Offline replays the full daily tape with the documented end-of-data "
        "policy. Historical stream replays session-by-session like paper trading.",
        "combo", "offline", ["offline", "historical-stream"],
    ),
    (
        "timeframe", "Timeframe",
        "Daily replay is the only supported stream timeframe.",
        "combo", "1d", ["1d"],
    ),
    (
        "start_date", "Start date",
        "YYYY-MM-DD first simulated session. Historical stream only.",
        "entry", "", None,
    ),
    (
        "end_date", "End date",
        "YYYY-MM-DD last simulated session. Use this or a session count.",
        "entry", "", None,
    ),
    (
        "session_count", "Session count",
        "Number of sessions to replay from the start date. 0 = use End date.",
        "spin", (0.0, 0.0, 5000.0, 1.0), None,
    ),
    (
        "warmup_bars", "Warmup bars",
        "Bars that build indicators before the first tradable session.",
        "spin", (40.0, 0.0, 400.0, 10.0), None,
    ),
    (
        "initial_capital", "Initial capital",
        "Paper-style account value for stream replay.",
        "spin", (100_000.0, 1_000.0, 100_000_000.0, 1_000.0), None,
    ),
    (
        "sizing_mode", "Sizing",
        "fixed-notional = flat position notional; paper-risk = the paper account's risk sizing.",
        "combo", "fixed-notional", ["fixed-notional", "paper-risk"],
    ),
    (
        "position_notional", "Position notional",
        "Flat notional per trade when sizing is fixed-notional.",
        "spin", (ENGINE.position_notional, 100.0, 1_000_000.0, 100.0), None,
    ),
    (
        "slippage_pct", "Slippage",
        "Per-fill slippage applied by the paper execution path.",
        "spin", (0.0005, 0.0, 0.05, 0.0001), None,
    ),
    (
        "pattern_only", "Pattern only",
        "Skip the portfolio/risk overlays and take every pattern signal.",
        "check", False, None,
    ),
    (
        "volume_gate", "Volume gate",
        "Require relative-volume / OBV confirmation before entry.",
        "check", False, None,
    ),
    (
        "kronos_gate", "Kronos gate",
        "Require the Kronos forecast to agree on direction.",
        "check", False, None,
    ),
    (
        "kronos_rank", "Kronos rank",
        "Enable the cross-sectional Kronos ranked sleeve.",
        "check", False, None,
    ),
    (
        "collect_first", "Collect first top N",
        "Collect signals and keep the best N by reward:risk. 0 = disabled.",
        "spin", (0.0, 0.0, 50.0, 1.0), None,
    ),
    (
        "end_policy", "End policy",
        "keep-open keeps positions open at data end (paper behavior); "
        "force-close labels the resulting exits.",
        "combo", "keep-open", ["keep-open", "force-close"],
    ),
]

ALL_PARAMS: list[tuple[str, str, str, str, Any, Optional[list[str]]]] = PARAMS + REPLAY_PARAMS


def _universe_for_pattern(pattern: Optional[str]) -> str:
    if not pattern:
        return "default"
    for key, uni in {
        "double_top": "double_top", "upward_channel": "upward_channel",
        "descending_channel": "upward_channel",
        "head_and_shoulders": "head_and_shoulders",
        "rounding_bottom": "rounding_bottom", "rounding_top": "rounding_bottom",
        "flag": "flag", "pennant": "pennant",
    }.items():
        if key in pattern.lower():
            return uni
    return "default"


def _default_for(entry) -> Any:
    _key, _label, _desc, ptype, default, _choices = entry
    if ptype == "spin":
        return default[0]
    if ptype == "check":
        return bool(default)
    return default


def normalize_values(
    raw: dict[str, Any],
    params=ALL_PARAMS,
) -> dict[str, Any]:
    """Coerce a posted/variable form dict to typed values; never raises."""
    out: dict[str, Any] = {}
    for entry in params:
        key, _label, _desc, ptype, default, _choices = entry
        if ptype == "check":
            value = raw.get(key)
            out[key] = str(value).lower() in ("1", "true", "on", "yes")
        elif ptype == "spin":
            try:
                out[key] = float(raw.get(key, _default_for(entry)))
            except (TypeError, ValueError):
                out[key] = float(_default_for(entry))
        elif ptype == "combo":
            value = str(raw.get(key) or "").strip()
            out[key] = value or _default_for(entry)
        else:
            value = str(raw.get(key) or "").strip()
            out[key] = value if value else None
    return out


def parse_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a YYYY-MM-DD date") from None


def settings_from_values(values: dict[str, Any]) -> BacktestSettings:
    """Build a validated ``BacktestSettings`` from normalized form values.

    Contradictory or unsupported combinations raise ``ValueError`` rather than
    silently substituting a default.
    """
    mode = str(values.get("mode") or "offline").strip()
    if mode not in ("offline", "historical-stream"):
        raise ValueError(f"Unsupported backtest mode: {mode}")
    timeframe = str(values.get("timeframe") or "1d").strip()
    if timeframe != "1d":
        raise ValueError("Only the 1d timeframe is supported")

    raw_symbols = values.get("symbols") or []
    if isinstance(raw_symbols, str):
        raw_symbols = raw_symbols.replace(",", " ").split()
    symbols = tuple(str(s).strip().upper() for s in raw_symbols if str(s).strip())
    universe = values.get("universe") or None
    if universe is not None:
        universe = str(universe).strip() or None
    if not symbols and values.get("chart_symbol"):
        from data.universes import load as load_universe

        if values.get("market") == "ph" and not universe:
            from data.history import list_history_symbols

            candidates = [row["symbol"].removesuffix(".PH")
                          for row in list_history_symbols(market="ph")]
        else:
            candidates = load_universe(universe or "default")
        symbols = tuple(dict.fromkeys([
            *candidates, str(values["chart_symbol"]).strip().upper()]))

    window = None
    if mode == "historical-stream":
        start = values.get("start_date")
        if not start:
            raise ValueError("Historical stream requires a start date")
        end = values.get("end_date") or None
        sessions = int(float(values.get("session_count") or 0))
        if end and sessions:
            raise ValueError("Specify either an end date or a session count, not both")
        if not end and not sessions:
            raise ValueError("Historical stream requires an end date or a session count")
        window = ReplayWindow(
            start_date=parse_date(start, "start_date"),
            end_date=parse_date(end, "end_date") if end else None,
            session_count=sessions or None,
            warmup_bars=int(float(values.get("warmup_bars") or 0)),
        )

    end_policy = "offline-legacy" if mode == "offline" else str(
        values.get("end_policy") or "keep-open"
    )
    execution = ExecutionSettings(
        initial_capital=float(values.get("initial_capital") or 100_000.0),
        sizing_mode=str(values.get("sizing_mode") or "fixed-notional"),
        position_notional=float(values.get("position_notional") or ENGINE.position_notional),
        txn_cost_pct=float(values.get("txn_cost_pct") or 0.0),
        slippage_pct=float(values.get("slippage_pct") or 0.0),
        pattern_only=bool(values.get("pattern_only")),
        volume_gate=bool(values.get("volume_gate")),
        kronos_gate=bool(values.get("kronos_gate")),
        kronos_rank=bool(values.get("kronos_rank")),
        collect_first=int(float(values.get("collect_first") or 0)),
        end_policy=end_policy,
    )
    return BacktestSettings(
        mode=mode,
        market=str(values.get("market") or default_market().id),
        timeframe=timeframe,
        symbols=symbols,
        universe=universe,
        window=window,
        execution=execution,
    )


def effective_parameters(settings: BacktestSettings, extra: dict | None = None) -> tuple[Parameter, ...]:
    """The frozen effective settings that distinguish one run from another."""
    execution = settings.execution
    values: list[tuple[str, Any]] = [
        ("initial_capital", execution.initial_capital),
        ("sizing_mode", execution.sizing_mode),
        ("position_notional", execution.position_notional),
        ("txn_cost_pct", execution.txn_cost_pct),
        ("slippage_pct", execution.slippage_pct),
        ("pattern_only", execution.pattern_only),
        ("volume_gate", execution.volume_gate),
        ("kronos_gate", execution.kronos_gate),
        ("kronos_rank", execution.kronos_rank),
        ("collect_first", execution.collect_first),
        ("end_policy", execution.end_policy),
        ("market", settings.market),
        ("timeframe", settings.timeframe),
        ("mode", settings.mode),
    ]
    for param in execution.parameters:
        values.append((param.name, param.value))
    for name, value in (extra or {}).items():
        values.append((name, value))
    return tuple(Parameter(name=name, value=value) for name, value in values)


def resolve_symbols(settings: BacktestSettings, extra_symbols: Any = None) -> list[str]:
    """Resolve the frozen universe: explicit symbols plus the named universe."""
    from data.universes import load as load_universe

    if settings.symbols:
        symbols = list(settings.symbols)
    else:
        symbols = list(load_universe(settings.universe or "default"))
    seen = set(symbols)
    for symbol in _extra_symbols(extra_symbols):
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if not symbols:
        raise ValueError("No symbols resolved for this backtest")
    return symbols


def _extra_symbols(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        chunks: list[str] = []
        for item in raw:
            chunks.extend(_extra_symbols(item))
        return chunks
    out = []
    for token in str(raw).replace(",", " ").split():
        symbol = token.strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def default_market_id() -> str:
    return get_market().id
