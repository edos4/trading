"""
core/engine_defaults.py — Single source of truth for entry/sizing/exit-engine
knobs used by Backtester, MarketScanner, and PaperAccount.

Issue this fixes: paper/live were missing min_confidence, SMA200 regime,
cooldown, and used a different max_position_pct than CLI/UI backtests —
so "validated" backtests did not describe what paper actually traded.

Callers must pull from ENGINE / helper functions rather than hardcoding
parallel constants in main.py, paper_trader.py, or backtest_dialog.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.market import format_money
from data.ohlcv_store import OHLCVStore
from patterns.base_pattern import TradeSignal
from utils.logger import log


@dataclass(frozen=True)
class EngineDefaults:
    """Canonical money-path defaults (CLI backtest + paper + scanner)."""

    min_confidence: float = 0.65
    regime_filter: bool = True
    # When True, skip min-confidence / min share-price / SMA200 / cooldown /
    # long-only. Kronos and volume gates stay independently toggled.
    pattern_only: bool = False
    # Allow BUY/SELL within this band of SMA200 (1.5% near-misses); still
    # block names 20%+ the wrong side of the average.
    regime_hysteresis_pct: float = 0.015
    cooldown_bars: int = 10
    txn_cost_pct: float = 0.001
    position_sizing: str = "risk"
    account_value: float = 100_000.0
    # 0.75% risk + 10% notional cap: 2%/6% hard-stop used to force 33% names.
    risk_per_trade_pct: float = 0.0075
    max_position_pct: float = 0.10
    # Long+short notional / equity. 0 = unlimited.
    max_gross_exposure_pct: float = 1.0
    trailing_activation_default: float = 0.02
    # US book defaults. PH overlays 5% / 0.80% via MarketProfile in
    # backtest_kwargs (PSE round-trip ~0.70% made a 0.15% floor a −0.6% tax).
    # Arm entry floor once +3%. 1.5% armed too early — 2026-08-18 paper had
    # five breakeven_stop exits at ~−0.1% after a brief +1.5% flicker.
    breakeven_trigger_pct: float | None = 0.03
    breakeven_buffer_pct: float = 0.0015
    min_atr_stop_multiple: float = 1.0
    synthetic_stop_multiple: float = 2.0
    atr_stop_floor_multiple: float = 1.2
    hard_stop_percentage: float = 0.06
    min_reward_risk_ratio: float = 1.5
    min_hold_bars: int = 2
    # Empty on purpose. 006/007 used to skip SMA200 (shorts of strength /
    # longs of weakness) and then dominated the losing paper book. They are
    # disabled by default; --pattern isolation still gets the regime filter.
    regime_exempt_patterns: tuple[str, ...] = ()


ENGINE = EngineDefaults()


def structure_filters_enabled(pattern_only: bool | None = None) -> bool:
    """False when Pattern-only is on — skip structure gates, keep Kronos/volume."""
    use = ENGINE.pattern_only if pattern_only is None else pattern_only
    return not bool(use)


def backtest_kwargs(**overrides: Any) -> dict[str, Any]:
    """Kwargs for Backtester(**...) matching the CLI money path.

    Includes max_open_positions from settings. Pass pattern_filter /
    disabled_patterns / kronos_gate / volume_gate via overrides.
    `market` overlays PH costs / capital / long-only when selected.
    """
    from config import settings
    from core.market import get_market

    d = asdict(ENGINE)
    d["max_open_positions"] = settings.max_open_positions
    # regime_hysteresis_pct is consumed by passes_regime_filter via ENGINE,
    # not Backtester.__init__. Drop it from constructor kwargs.
    d.pop("regime_hysteresis_pct", None)
    d.pop("regime_exempt_patterns", None)
    market = overrides.pop("market", None)
    profile = get_market(market)
    d["market"] = profile.id
    d["txn_cost_pct"] = profile.txn_cost_pct
    d["breakeven_trigger_pct"] = profile.breakeven_trigger_pct
    d["breakeven_buffer_pct"] = profile.breakeven_buffer_pct
    d["account_value"] = profile.paper_initial_capital
    d["long_only"] = profile.long_only
    d["min_share_price"] = profile.min_share_price
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


def describe_confidence_rejection(
    signal: TradeSignal,
    min_confidence: float | None = None,
) -> str:
    thresh = ENGINE.min_confidence if min_confidence is None else min_confidence
    return (
        f"Min-confidence gate: pattern scored {signal.confidence:.2f} but the "
        f"engine floor is {thresh:.2f} — setup too weak to trade."
    )


def passes_min_share_price(
    signal: TradeSignal,
    min_share_price: float | None = None,
) -> bool:
    return describe_min_share_price_rejection(signal, min_share_price) is None


def describe_min_share_price_rejection(
    signal: TradeSignal,
    min_share_price: float | None = None,
    *,
    market: str | None = None,
) -> str | None:
    """Block entries on sub-floor share prices (gap/wick risk on OTC-style names)."""
    if min_share_price is None or min_share_price <= 0:
        return None
    if signal.action == "CLOSE" or signal.price is None or signal.price <= 0:
        return None
    if signal.price < min_share_price:
        px = format_money(signal.price, market)
        floor = format_money(min_share_price, market)
        log.debug(
            f"EntryGate | {signal.symbol} {signal.pattern} price "
            f"{px} < min {floor} — skip"
        )
        return (
            f"Min share-price gate: {signal.symbol} entry {px} is "
            f"below the {floor} floor — names under that price are excluded "
            f"after gap-risk losses on the US paper book."
        )
    return None


def passes_regime_filter(
    signal: TradeSignal,
    store: OHLCVStore,
    *,
    enabled: bool | None = None,
) -> bool:
    """BUY only above SMA200 (within hysteresis), SELL only below. No-op if <200 bars."""
    return describe_regime_rejection(signal, store, enabled=enabled) is None


def describe_regime_rejection(
    signal: TradeSignal,
    store: OHLCVStore,
    *,
    enabled: bool | None = None,
    market: str | None = None,
) -> str | None:
    """Human-readable regime reject, or None if the signal clears the filter."""
    use = ENGINE.regime_filter if enabled is None else enabled
    if not use or signal.action == "CLOSE":
        return None
    if signal.pattern in ENGINE.regime_exempt_patterns:
        return None
    df = store.get_df(signal.symbol, signal.timeframe, min_bars=1)
    if df is None or len(df) < 200:
        return None
    close = df["close"]
    sma200 = close.rolling(200).mean()
    current_sma200 = float(sma200.iloc[-1])
    current_close = float(close.iloc[-1])
    if current_sma200 <= 0:
        return None
    pct_vs = (current_close - current_sma200) / current_sma200 * 100.0
    band = ENGINE.regime_hysteresis_pct * 100.0
    close_txt = format_money(current_close, market)
    sma_txt = format_money(current_sma200, market)

    if signal.action == "BUY" and pct_vs < -band:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} BUY below SMA200 — skip"
        )
        return (
            f"SMA200 regime filter: longs only when price is above the 200-bar SMA "
            f"(treat as uptrend). {signal.symbol} {signal.timeframe} close "
            f"{close_txt} is {abs(pct_vs):.2f}% below SMA200 "
            f"{sma_txt} — counter-trend BUY blocked."
        )
    if signal.action == "SELL" and pct_vs > band:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} SELL above SMA200 — skip"
        )
        return (
            f"SMA200 regime filter: shorts only when price is below the 200-bar SMA "
            f"(treat as downtrend). {signal.symbol} {signal.timeframe} close "
            f"{close_txt} is {pct_vs:.2f}% above SMA200 "
            f"{sma_txt} — counter-trend SELL blocked."
        )
    return None


def passes_cooldown(
    signal: TradeSignal,
    bar_idx: int,
    cooldown_tracker: dict[tuple[str, str], tuple[int, bool]],
    *,
    cooldown_bars: int | None = None,
) -> bool:
    """Block re-entry into a symbol after any losing exit within cooldown_bars."""
    return describe_cooldown_rejection(
        signal, bar_idx, cooldown_tracker, cooldown_bars=cooldown_bars,
    ) is None


def describe_cooldown_rejection(
    signal: TradeSignal,
    bar_idx: int,
    cooldown_tracker: dict[tuple[str, str], tuple[int, bool]],
    *,
    cooldown_bars: int | None = None,
) -> str | None:
    """Human-readable cooldown reject, or None if the signal clears cooldown."""
    n = ENGINE.cooldown_bars if cooldown_bars is None else cooldown_bars
    if n <= 0:
        return None
    latest_loss_bar: int | None = None
    latest_pattern = signal.pattern
    for (sym, pat), (exit_bar, was_loss) in cooldown_tracker.items():
        if sym != signal.symbol or not was_loss:
            continue
        if latest_loss_bar is None or exit_bar >= latest_loss_bar:
            latest_loss_bar = exit_bar
            latest_pattern = pat
    if latest_loss_bar is None:
        return None
    bars_since = bar_idx - latest_loss_bar
    if bars_since < n:
        log.debug(
            f"EntryGate | {signal.symbol} {signal.timeframe} cooldown "
            f"{bars_since}/{n} bars — skip"
        )
        return (
            f"Post-loss cooldown: last {latest_pattern} trade on {signal.symbol} "
            f"was a loss; only {bars_since} of {n} required bars have printed "
            f"since that exit — re-entry blocked to avoid chopping the same name."
        )
    return None


def seed_cooldown_from_trades(
    tracker: dict[tuple[str, str], tuple[int, bool]],
    trades: list[Any],
) -> None:
    """Replay closed trades into a cooldown map (latest exit per symbol+pattern)."""
    for trade in trades:
        symbol = getattr(trade, "symbol", None)
        pattern = getattr(trade, "pattern", None)
        exit_bar = getattr(trade, "exit_bar_idx", None)
        if not symbol or not pattern or exit_bar is None:
            continue
        key = (str(symbol), str(pattern))
        prev = tracker.get(key)
        bar = int(exit_bar)
        if prev is None or bar >= prev[0]:
            tracker[key] = (bar, bool(getattr(trade, "pnl", 0) < 0))
