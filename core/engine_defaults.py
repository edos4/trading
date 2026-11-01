"""
core/engine_defaults.py — the small shared money-path config for the offline
pattern-backtest engine and the paper trader.

History: this file used to carry ~40 knobs (risk sizing, ATR stop floors,
min-confidence, SMA200 regime, cooldown, breakeven / profit-lock / dead-trade
overlays, a compounding capital ledger). The 2026-09 refactor stripped all of
that: the engine now runs each pattern's own conditions and its own exit ladder,
sizes every trade at a flat notional, and treats trades as independent — matching
the locked `.cjs` pattern-backtest scripts. What survives is just enough for the
backtester and PaperAccount to stay byte-identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EngineConfig:
    """Canonical money-path constants (offline backtest + paper trader)."""

    # Flat notional per trade — no risk sizing, no compounding.
    position_notional: float = 10_000.0
    # 0.0 keeps the flat-notional path above (`.cjs` parity). Set to a fraction
    # of equity — 0.005 = 0.5% — to size each trade so the distance to the
    # effective initial stop equals that fraction of equity. Needed once stops
    # are volatility-scaled: with a flat notional, dollar risk varies with the
    # stop distance, so a 3% stop on a 10%-ATR name risks three times what an
    # 8% stop on a quiet name does.
    risk_pct: float = 0.0
    # Round-trip cost per leg. 0.0 = the documented `.cjs` headline numbers
    # (which carry no cost line). `--txn-cost 0.001` matches the `.cjs`
    # "cost optional" mode; PH overlays its own via MarketProfile.
    txn_cost_pct: float = 0.0
    # A symbol needs at least this many daily bars to be simulated
    # (`.cjs`: skip `< 100`).
    min_bars: int = 100
    # Patterns sized with fractional shares (`.cjs` `10000/entry`); every other
    # pattern floors to whole shares (`.cjs` `Math.floor(10000/entry)`).
    fractional_qty_patterns: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "pattern_004_rounding_bottom",
            "pattern_009_flag_pattern",
            "pattern_010_pennant",
        })
    )


ENGINE = EngineConfig(
    # Per-process override so a paper run can size by risk without editing the
    # frozen settings or the systemd unit: TRADING_RISK_PCT=0.005 => 0.5%.
    risk_pct=float(os.environ.get("TRADING_RISK_PCT", "0") or 0),
)


def is_fractional_qty(pattern: str) -> bool:
    return pattern in ENGINE.fractional_qty_patterns


# ── Back-compat shims ────────────────────────────────────────────────────────
# The web services (web/services.py) annotate a chart-scan with
# "would this signal pass the gates". There are no gates anymore, so these
# always pass. Kept as no-ops so those modules import without a rewrite.
def passes_min_confidence(*_a, **_k) -> bool:
    return True


def passes_regime_filter(*_a, **_k) -> bool:
    return True
