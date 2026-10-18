"""Shared decisions for causal paper and historical-stream execution."""
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


def opening_candidate(signal, candle, notional, market, slippage=0):
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
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Execution open must be finite and positive')
    for value in (signal.stop_loss, signal.take_profit):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('Stop and target must be finite and positive')
    short = signal.action == 'SELL'
    if signal.stop_loss is not None and (price >= signal.stop_loss if short else price <= signal.stop_loss):
        raise ValueError('Opening gap crossed the structural stop')
    if signal.take_profit is not None and (price <= signal.take_profit if short else price >= signal.take_profit):
        raise ValueError('Opening gap crossed the target')
    fill = replace(signal, price=price)
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
