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
    # Post stop_loss on same symbol (any pattern). 10 bars let CSL/DHI chop
    # 0/3 and 1/3 for −$805/−$884; raised to 20 after 2026-09-01 review.
    cooldown_bars: int = 20
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
    breakeven_trigger_pct: float | None = 0.06
    breakeven_buffer_pct: float = 0.0015
    min_atr_stop_multiple: float = 1.0
    synthetic_stop_multiple: float = 2.0
    atr_stop_floor_multiple: float = 1.2
    # Backstop only — NOT the intended per-trade risk. 2026-08-30 paper (41
    # closed US trades) showed 40/41 stops landing at exactly this cap: every
    # pattern's structural stop (007 SL2-low×0.99, 003 below-L2) is wider than
    # 6% in practice, so this constant silently overrode the pattern's own
    # stop on almost every trade instead of only catching the rare missing/
    # runaway one. Position sizing already targets a fixed risk_per_trade_pct
    # off the *actual* stop distance (_apply_sizing: qty = risk_$ / stop_
    # distance), so a wider stop does not add dollar risk — it just sizes the
    # trade smaller and gives the thesis room to breathe. The tight 6% cap
    # instead maximized whipsaw: stop_loss exits were 13/41 (32%) of trades,
    # 100% losers, avg hold 2.8 bars, -$7,710 — the single largest drag on the
    # book, versus +$3,748/+$8,680 from profit_lock/take_profit exits that
    # were structurally allowed to run. Raised to 0.12 so it still blocks a
    # true tail-risk stop (bad print / no structural stop) without clamping
    # every ordinary swing-pattern stop to the same distance regardless of
    # the name's actual volatility or structure.
    hard_stop_percentage: float = 0.12
    min_reward_risk_ratio: float = 1.5
    min_hold_bars: int = 2
    # Hard winner cap from entry (0.08 = close at +8% unrl). Off by default:
    # a flat take fights pattern measured-move targets and trail-only setups
    # (009/010). Use profit_lock_frac to cap *giveback* instead of upside.
    # 0 / None = off.
    profit_take_pct: float | None = None
    # Ratcheting profit floor: once peak close-to-close unrl (_best_pnl_pct)
    # clears the trade's own trigger (see profit_lock_trigger_r below),
    # protective stop = entry × (1 ± best × frac) for long/short. Never
    # loosens as best only rises. Caps giveback (e.g. +10% MFE → floor at
    # +2.5% with frac=0.25) without capping upside. 0 / None = off.
    profit_lock_frac: float | None = 0.25
    # Do not arm the ratchet until peak unrl reaches this many multiples of
    # the trade's OWN initial risk (entry-to-stop distance, i.e. "R").
    # 2026-08-30 paper (24 closed US trades): the old knob was a flat
    # +3% price move (profit_lock_trigger_pct=0.03) regardless of stop
    # distance — the floor started ratcheting up before a trade had earned
    # back a fraction of what it was risking, let alone approached its
    # 1.5-3.6R structural target. Result: winners cut short, losers run full.
    # 0.4R (~+4% on a 10% stop) still armed too early: the 2026-09-01 paper
    # stream clipped both winners at avg +0.36R (EZPW +0.27R, ATLX +0.45R
    # after peaking +0.91R) while stop_loss losers averaged −1.0R — a
    # structurally negative asymmetry for a 3.6R-target swing setup. Require
    # +1.0R of proof before arming, then lock only frac=0.25 of whatever
    # peak follows, so a trade must actually work (not just flicker green)
    # before giveback gets trimmed and real winners keep room to run.
    # 0 / None = arm on any positive MFE (the old too-eager behavior).
    profit_lock_trigger_r: float | None = 1.0
    # Fill on the signal bar; if the next session closes against entry, exit
    # at that close (bar 1). Not deferred entry — keeps STAA-style bar-1
    # take-profits while dumping ICLR-style gap losers before the 10% stop.
    # Legal on bar 1 (min_hold_bars does not apply).
    first_bar_invalidation_enabled: bool = True
    # Backstop: 49 trades never printed MFE > 0.15% through bar 3 (WR 8%,
    # −$14,687). Flatten at bar-3 close saves ~$3,500 vs the 8-bar time stop.
    dead_trade_flatten_bars: int = 3
    dead_trade_mfe_threshold_pct: float = 0.0015
    # Empty on purpose. 006/007 used to skip SMA200 (shorts of strength /
    # longs of weakness) and then dominated the losing paper book. They are
    # disabled by default; --pattern isolation still gets the regime filter.
    regime_exempt_patterns: tuple[str, ...] = ()
    # Position-sizing floor for gap/tail risk. "risk" sizing normally divides
    # risk_$ by the *structural* stop distance, which assumes the stop can
    # actually be hit before further slippage. Illiquid/volatile names can
    # gap straight through it overnight (2026-08-31 paper: AARD, ~$6.73
    # entry, 10% structural stop, opened -35% the next session — a name
    # whose own ATR% was already several multiples of the nominal stop
    # distance). Sizing now also floors the effective stop distance at
    # ATR(14) x this multiple, so names with outsized realized volatility
    # get a smaller position instead of a full-size bet on a stop that
    # volatility says is unlikely to hold. Does not change the placed
    # stop_loss price — only how many shares that risk buys.
    gap_risk_atr_multiple: float = 2.5


ENGINE = EngineDefaults()


# Patterns whose entries are defined *relative to trend* (breakout of a
# channel drawn against the prevailing direction) rather than a reversal
# structure. 2026-01 paper review: running these with the SMA200 regime
# filter off (pattern_only=True skips it for every pattern, not just the
# ones regime_exempt_patterns names) let 007 alone account for 79/110
# (72%) of all trades in the 2026-08-31 patterns-only run, at a 30% win
# rate and 0.74 profit factor — the single largest drag on the book,
# versus 57% win / 2.60 pf for 003 in the same run. The regime filter is
# therefore mandatory for these patterns even when Pattern-only is set;
# Pattern-only still isolates confidence/long-only for them as before.
REGIME_REQUIRED_PATTERNS: tuple[str, ...] = (
    "pattern_006_upward_channel",
    "pattern_007_descending_channel",
)


def structure_filters_enabled(pattern_only: bool | None = None) -> bool:
    """False when Pattern-only is on — skip structure gates, keep Kronos/volume."""
    use = ENGINE.pattern_only if pattern_only is None else pattern_only
    return not bool(use)


def regime_filter_required(
    pattern_name: str | None,
    pattern_only: bool | None = None,
) -> bool:
    """True if the SMA200 regime gate must run for this signal.

    Same as structure_filters_enabled() for most patterns, but always True
    for REGIME_REQUIRED_PATTERNS (006/007) regardless of Pattern-only — see
    the comment on that constant. Pattern-only still isolates confidence/
    long-only for those patterns; only the regime gate is forced back on.
    """
    if structure_filters_enabled(pattern_only):
        return True
    return pattern_name in REGIME_REQUIRED_PATTERNS


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


def signal_reward_risk(signal: TradeSignal) -> float | None:
    """Reward:risk of a signal from price/stop/target; None if not computable.

    Matches the R:R the backtester/risk-gate use after stop backstops finalize
    ``stop_loss`` (core/backtester.py describe_risk_gate_rejection). Used by the
    collect-first pipeline to rank chart-pattern signals.
    """
    if (
        signal.price is None
        or signal.price <= 0
        or signal.stop_loss is None
        or signal.take_profit is None
    ):
        return None
    reward = abs(signal.take_profit - signal.price)
    risk = abs(signal.price - signal.stop_loss)
    if risk <= 0:
        return None
    return reward / risk


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
