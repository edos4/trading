"""
core/engine_defaults.py — Single source of truth for entry/sizing/exit-engine
knobs used by Backtester, MarketScanner, and PaperAccount.

Issue this fixes: paper/live were missing min_confidence, SMA200 regime,
cooldown, and used max_position_pct=0.10 while CLI/UI backtests used 0.33 —
so "validated" backtests did not describe what paper actually traded.

Callers must pull from ENGINE / helper functions rather than hardcoding
parallel constants in main.py, paper_trader.py, or backtest_dialog.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from data.ohlcv_store import OHLCVStore
from patterns.base_pattern import TradeSignal
from utils.logger import log


@dataclass(frozen=True)
class EngineDefaults:
    """Canonical money-path defaults (CLI backtest + paper + scanner)."""

    min_confidence: float = 0.6
    regime_filter: bool = True
    cooldown_bars: int = 10
    txn_cost_pct: float = 0.001
    position_sizing: str = "risk"
    account_value: float = 100_000.0
    risk_per_trade_pct: float = 0.02
    # 0.33 lets 2% risk bind against a ~6% hard stop (0.02/0.06).
    max_position_pct: float = 0.33
    trailing_activation_default: float = 0.02
    breakeven_trigger_pct: float | None = None
    breakeven_buffer_pct: float = 0.0015
    min_atr_stop_multiple: float = 1.0
    synthetic_stop_multiple: float = 2.0
    atr_stop_floor_multiple: float = 1.2
    hard_stop_percentage: float = 0.06
    min_reward_risk_ratio: float = 1.5
    min_hold_bars: int = 2


ENGINE = EngineDefaults()


def backtest_kwargs(**overrides: Any) -> dict[str, Any]:
    """Kwargs for Backtester(**...) matching the CLI money path.

    Includes max_open_positions from settings. Pass pattern_filter /
    disabled_patterns / kronos_gate / volume_gate via overrides.
    """
    from config import settings

    d = asdict(ENGINE)
    d["max_open_positions"] = settings.max_open_positions
    d.update(overrides)
    return d


def risk_gate_kwargs(**overrides: Any) -> dict[str, Any]:
    """Subset passed to apply_risk_gates() — paper and backtest must match."""
    keys = (
        "min_atr_stop_multiple",
        "synthetic_stop_multiple",
        "atr_stop_floor_multiple",
        "hard_stop_percentage",
        "min_reward_risk_ratio",
        "trailing_activation_default",
    )
    d = {k: getattr(ENGINE, k) for k in keys}
    d.update(overrides)
    return d


def sizing_kwargs(*, account_value: float, **overrides: Any) -> dict[str, Any]:
    """Subset passed to _apply_sizing() — paper and backtest must match."""
    d = {
        "account_value": account_value,
        "risk_per_trade_pct": ENGINE.risk_per_trade_pct,
        "position_sizing": ENGINE.position_sizing,
        "max_position_pct": ENGINE.max_position_pct,
    }
    d.update(overrides)
    return d


def passes_min_confidence(
    signal: TradeSignal,
    min_confidence: float | None = None,
) -> bool:
    thresh = ENGINE.min_confidence if min_confidence is None else min_confidence
    if signal.confidence < thresh:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.pattern} confidence "
            f"{signal.confidence:.2f} < min {thresh:.2f} — skip"
        )
        return False
    return True


def passes_regime_filter(
    signal: TradeSignal,
    store: OHLCVStore,
    *,
    enabled: bool | None = None,
) -> bool:
    """BUY only above SMA200, SELL only below. No-op if <200 bars."""
    use = ENGINE.regime_filter if enabled is None else enabled
    if not use or signal.action == "CLOSE":
        return True
    df = store.get_df(signal.symbol, signal.timeframe, min_bars=1)
    if df is None or len(df) < 200:
        return True
    close = df["close"]
    sma200 = close.rolling(200).mean()
    current_sma200 = float(sma200.iloc[-1])
    current_close = float(close.iloc[-1])
    if signal.action == "BUY" and current_close < current_sma200:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} BUY below SMA200 — skip"
        )
        return False
    if signal.action == "SELL" and current_close > current_sma200:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} SELL above SMA200 — skip"
        )
        return False
    return True


def passes_cooldown(
    signal: TradeSignal,
    bar_idx: int,
    cooldown_tracker: dict[tuple[str, str], tuple[int, bool]],
    *,
    cooldown_bars: int | None = None,
) -> bool:
    """Block re-entry into (symbol, pattern) after a loss within cooldown_bars."""
    n = ENGINE.cooldown_bars if cooldown_bars is None else cooldown_bars
    if n <= 0:
        return True
    key = (signal.symbol, signal.pattern)
    if key not in cooldown_tracker:
        return True
    exit_bar, was_loss = cooldown_tracker[key]
    bars_since = bar_idx - exit_bar
    if was_loss and bars_since < n:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} cooldown "
            f"{bars_since}/{n} bars — skip"
        )
        return False
    return True
