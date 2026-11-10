"""Shared decisions for causal paper and historical-stream execution."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

from core.engine_defaults import is_fractional_qty
from core.market import apply_lot_rounding, get_market


def stable_anchors(signal, store):
    """Persist session identities instead of indices that shift on eviction."""
    if not signal.setup_key:
        signal.setup_key = (int(signal.signal_bar_timestamp.timestamp() * 1_000_000),)
        return
    candles = store.copy_candles(signal.symbol, signal.timeframe)
    signal.setup_key = tuple(
        int(candles[i].timestamp.timestamp() * 1_000_000)
        if 0 <= i < len(candles) and candles[i].timestamp else i
        for i in signal.setup_key
    )


def rank_candidates(pool, limit):
    """Scanner ordering: one symbol/timeframe, best R:R, confidence, symbol."""
    def rr(signal):
        if not signal.price or signal.stop_loss is None or signal.take_profit is None:
            return None
        risk = abs(signal.price - signal.stop_loss)
        return abs(signal.take_profit - signal.price) / risk if risk else None
    best = {}
    for item in pool:
        signal = item[0]
        key = (signal.symbol, signal.timeframe)
        old = best.get(key)
        value = rr(signal)
        if old is None or (value is not None and (rr(old[0]) is None or value > rr(old[0]))):
            best[key] = item
    ranked = sorted(best.values(), key=lambda item: (
        rr(item[0]) is None, -(rr(item[0]) or 0), -float(item[0].confidence), item[0].symbol))
    return ranked[:limit], ranked[limit:]


def _effective_stop_for(signal, price):
    """Nearer-to-entry of the structural stop and the fixed % cap at ``price``."""
    base = signal.stop_loss
    cap = None
    if signal.stop_loss_pct_cap is not None and price > 0:
        c = signal.stop_loss_pct_cap
        cap = price * (1 + c) if signal.action == "SELL" else price * (1 - c)
    if base is None:
        return cap
    if cap is None:
        return base
    return min(base, cap) if signal.action == "SELL" else max(base, cap)


def _planned_risk_frac(signal):
    """Stop distance the signal intended, as a fraction of its own price."""
    if not signal.price or signal.price <= 0:
        return None
    stop = _effective_stop_for(signal, signal.price)
    if stop is None:
        return None
    return abs(signal.price - stop) / signal.price


def entry_risk_geometry(signal, price: float, *, paper_costs: tuple[float, float] | None = None) -> dict:
    """Describe gross trigger risk separately from the cost-aware net budget."""
    from core.position_protection import LOSS_LIMIT_PCT, paper_stop_price

    pattern = _effective_stop_for(signal, price)
    paper = paper_stop_price(price, signal.action, *paper_costs) if paper_costs is not None else None
    levels = [stop for stop in (pattern, paper) if stop is not None]
    initial = (min(levels) if signal.action == 'SELL' else max(levels)) if levels else None
    return {'schema_version': 1, 'risk_units': 'gross_price_distance',
            'pattern_stop': pattern, 'paper_initial_stop': paper, 'initial_gross_stop': initial,
            'pattern_distance': abs(price-pattern) if pattern is not None else None,
            'paper_initial_distance': abs(price-initial) if initial is not None else None,
            'paper_net_loss_budget_pct': LOSS_LIMIT_PCT if paper is not None else None,
            'fee_per_side': paper_costs[0] if paper_costs is not None else None,
            'slippage_per_side': paper_costs[1] if paper_costs is not None else None}


def apply_target_cap(signal, price: float, max_r: float, basis: str, *,
                     paper_costs: tuple[float, float] | None = None) -> dict:
    """Apply the same explicit R definition in execution and counterfactuals."""
    if basis not in ('pattern', 'paper_initial'):
        raise ValueError('Unknown target risk basis')
    if not math.isfinite(max_r) or max_r < 0:
        raise ValueError('Target cap must be finite and nonnegative')
    geometry = entry_risk_geometry(signal, price, paper_costs=paper_costs)
    distance = geometry[basis + '_distance']
    if max_r > 0:
        if basis == 'paper_initial' and paper_costs is None:
            raise ValueError('Paper-initial target R requires paper execution costs')
        if distance is None or distance <= 0:
            raise ValueError('Target cap requires positive initial risk')
        sign = -1 if signal.action == 'SELL' else 1
        target = price + sign * max_r * distance
        if target <= 0:
            raise ValueError('Target cap would produce a nonpositive target')
        if signal.take_profit is not None and sign*(signal.take_profit-target) > 0:
            signal.take_profit = round(target, 4)
    geometry.update(target_risk_basis=basis, target_risk_distance=distance, target_max_r=max_r)
    return geometry


def validate_entry_geometry(signal, price: float, *, opening: bool = False) -> None:
    """Reject inverted protection instead of silently opening an unprotected bet."""
    if signal.action not in ("BUY", "SELL"):
        raise ValueError("Trade entries require BUY or SELL")
    if price is None or not math.isfinite(price) or price <= 0:
        raise ValueError("Entry price must be finite and positive")
    for value in (signal.stop_loss, signal.take_profit):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Stop and target must be finite and positive")
    cap = signal.stop_loss_pct_cap
    if cap is not None and (not math.isfinite(cap) or not 0 < cap < 1):
        raise ValueError("Stop cap must be finite and between zero and one")
    short = signal.action == "SELL"
    prefix = "Opening gap" if opening else "Entry price"
    if signal.stop_loss is not None and (price >= signal.stop_loss if short else price <= signal.stop_loss):
        raise ValueError(f"{prefix} crossed the structural stop")
    if signal.take_profit is not None and (price <= signal.take_profit if short else price >= signal.take_profit):
        raise ValueError(f"{prefix} crossed the target")
    if signal.provenance == 'patternsv2':
        evidence = getattr(signal, 'pattern_evidence', None) or {}
        policy = evidence.get('entry_boundary_policy')
        if policy is not None and policy != 'frozen-breakout-v1':
            raise ValueError('Unknown Patternsv2 entry-boundary policy')
        boundary = evidence.get('breakout_level')
        if boundary is None and policy is None:
            # Older pending V2 orders already contain their frozen level.
            levels = {a.get('price') for a in signal.chart_annotations
                      if a.get('type') == 'hline' and a.get('label') == 'level'}
            if len(levels) == 1:
                boundary = next(iter(levels))
        if (isinstance(boundary, bool) or not isinstance(boundary, (int, float))
                or not math.isfinite(boundary) or boundary <= 0):
            raise ValueError('Patternsv2 breakout boundary evidence is required')
        if evidence.get('direction', -1 if short else 1) != (-1 if short else 1):
            raise ValueError('Patternsv2 breakout direction differs from the entry')
        if (price > boundary if short else price < boundary):
            raise ValueError(f'{prefix} returned inside the Patternsv2 breakout boundary')


def opening_candidate(signal, candle, notional, market, slippage=0, *, paper_costs=None):
    """Fixed structural levels; cancel gaps beyond them rather than erase risk."""
    from core.backtester import _apply_notional_sizing
    from zoneinfo import ZoneInfo

    if signal.signal_bar_timestamp is None or candle.timestamp is None:
        raise ValueError('Pending entry requires aware signal and execution timestamps')
    if signal.signal_bar_timestamp.tzinfo is None or candle.timestamp.tzinfo is None:
        raise ValueError('Pending entry timestamps must include a timezone')
    if candle.timestamp <= signal.signal_bar_timestamp:
        return None
    # shortcut: a missing symbol tape expires after seven calendar days (14 for
    # weekly bars); use an exchange calendar if longer suspensions are required.
    ttl = 14 if signal.timeframe.lower() in ('1w', 'w') else 7
    if candle.timestamp - signal.signal_bar_timestamp > timedelta(days=ttl):
        raise ValueError('Pending entry expired before the next observed bar')
    if signal.action not in ("BUY", "SELL"):
        raise ValueError("Trade entries require BUY or SELL")
    from core.pattern_provenance import engine_hash
    saved_engine = (signal.requested_rules or {}).get("engine_sha256")
    if saved_engine is not None and saved_engine != engine_hash():
        raise ValueError("Pending entry runtime changed; candidate rejected")
    price = candle.open * (1 - slippage if signal.action == "SELL" else 1 + slippage)
    # Adverse slippage cannot turn an unconfirmed raw open into a breakout.
    validate_entry_geometry(signal, candle.open, opening=True)
    validate_entry_geometry(signal, price, opening=True)
    short = signal.action == 'SELL'
    from config import settings

    effective_stop = _effective_stop_for(signal, price)
    risk = (
        abs(price - effective_stop) / price
        if effective_stop is not None and price > 0 else None
    )
    # The fixed stop is anchored to the signal close while the fill is the next
    # open. An adverse gap can leave a nearly zero risk distance (the 2026-11
    # export had a 0.03% stop), which is a coin flip, not a trade.
    planned_risk = _planned_risk_frac(signal)
    min_frac = float(settings.stop_loss_min_risk_frac or 0.0)
    if (
        risk is not None
        and signal.stop_loss_pct_cap is not None
        and planned_risk is not None
        and min_frac > 0
        and risk < min_frac * planned_risk
    ):
        raise ValueError(
            f"Opening gap left {risk:.2%} to the stop, below "
            f"{min_frac:.0%} of the {planned_risk:.2%} planned risk"
        )
    fill = replace(signal, price=price)
    if risk is not None and risk > 0:
        # Preserve the signal's ladder rather than re-reading today's global
        # legacy settings. V2 expresses its arm/giveback in units of the ATR
        # cap; a nearer structural stop or opening gap changes the actual R.
        basis = (signal.stop_loss_pct_cap if signal.provenance == "patternsv2"
                 else planned_risk)
        if basis is not None and basis > 0:
            for field in ("trailing_stop_pct", "trailing_activation_pct"):
                value = getattr(signal, field)
                if value is not None:
                    setattr(fill, field, round(value * risk / basis, 6))
    fill.execution_risk = apply_target_cap(fill, price, float(settings.target_max_r or 0),
                                           settings.target_risk_basis, paper_costs=paper_costs)
    _apply_notional_sizing(fill, notional, fractional=is_fractional_qty(fill.pattern))
    profile = get_market(market)
    if profile.long_only and short:
        raise ValueError('Short entries are disabled in this market')
    if profile.lot_round and not apply_lot_rounding(
            fill, as_of=candle.timestamp.astimezone(ZoneInfo(profile.session_tz)).date()):
        raise ValueError('Opening quantity is below one board lot')
    if fill.qty <= 0 or (fill.qty < 1 and not is_fractional_qty(fill.pattern)):
        raise ValueError('Opening quantity is below the minimum')
    return fill


def opening_observation(candle, market):
    """Only the regular-session opening price is available for a fill recheck."""
    from zoneinfo import ZoneInfo
    local = candle.timestamp.astimezone(ZoneInfo(get_market(market).session_tz))
    return replace(candle, high=candle.open, low=candle.open, close=candle.open, volume=0,
                   timestamp=local.replace(hour=9, minute=30, second=0, microsecond=0))
