"""Cost-aware paper stops, ratcheted from surviving closes and live prices.

The budget is a fraction of entry notional, not account equity. A stop is
an order trigger, so a gap can still exceed the budget. Pattern orders remain
active and can close a trade earlier than this independent safety order.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.backtester import BacktestTrade
    from data.tv_client import OHLCVCandle

LOSS_LIMIT_PCT = 0.03
TRAIL_GIVEBACK_PCT = 0.03


def _net_return(position: BacktestTrade, raw_price: float) -> float:
    policy = position.paper_protection
    short = position.action == "SELL"
    fill = raw_price * (1 + policy["slippage_pct"] if short else 1 - policy["slippage_pct"])
    move = position.entry_price - fill if short else fill - position.entry_price
    fees = (position.entry_price + fill) * policy["txn_cost_pct"]
    return (move - fees) / position.entry_price


def _trigger(position: BacktestTrade, floor: float) -> float:
    policy = position.paper_protection
    return paper_stop_price(position.entry_price, position.action, policy['txn_cost_pct'],
                            policy['slippage_pct'], floor)


def paper_stop_price(entry: float, action: str, fee: float, slip: float,
                     floor: float = -LOSS_LIMIT_PCT) -> float:
    """Raw trigger for a net return floor, shared by entry and exit arithmetic."""
    if (action not in ('BUY', 'SELL') or not math.isfinite(entry) or entry <= 0
            or not math.isfinite(floor) or not math.isfinite(fee) or not 0 <= fee < 1
            or not math.isfinite(slip) or not 0 <= slip < 1):
        raise ValueError('Invalid paper stop economics')
    if action == "SELL":
        return entry * (1 - fee - floor) / ((1 + fee) * (1 + slip))
    return entry * (1 + fee + floor) / ((1 - fee) * (1 - slip))


def ensure_paper_protection(
    position: BacktestTrade, txn_cost_pct: float, slippage_pct: float,
    as_of: datetime, *, at_entry: bool = False, entry_market_price: float | None = None,
) -> None:
    """Install once; adopt old open positions without rewriting their history.

    Only marks strictly before the managed bar may seed an adopted trail.
    Existing policy state survives restarts and never reads future marks.
    """
    if position.paper_protection is not None:
        return
    if not (math.isfinite(txn_cost_pct) and 0 <= txn_cost_pct < 1
            and math.isfinite(slippage_pct) and 0 <= slippage_pct < 1):
        raise ValueError("Paper protection requires fee/slippage rates between zero and one")
    position.paper_protection = {
        "policy": "net-loss-trail-v1",
        "loss_limit_pct": LOSS_LIMIT_PCT,
        "giveback_pct": TRAIL_GIVEBACK_PCT,
        "txn_cost_pct": txn_cost_pct,
        "slippage_pct": slippage_pct,
        "adopted_at": as_of.isoformat(),
        "peak_net_pct": 0.0,
    }
    stop = _trigger(position, -LOSS_LIMIT_PCT)
    position.paper_protection["stop"] = stop
    # Preserve historical R on adopted positions; new positions report the
    # actual initial protection rather than the wider detector's ATR risk.
    position.paper_protection["initial_stop"] = stop if at_entry else None
    if (at_entry and entry_market_price is not None
            and _net_return(position, entry_market_price) <= -LOSS_LIMIT_PCT):
        position.paper_protection = None
        raise ValueError("Entry execution costs already exhaust the 3% paper loss budget")
    if not at_entry:
        for mark in position.position_marks:
            stamp = mark.get("sim_bar")
            if not stamp or datetime.fromisoformat(stamp) >= as_of:
                continue
            price = (mark.get("exit_check") or {}).get("ohlc", {}).get("close", mark.get("close"))
            if isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
                ratchet_paper_protection(position, price)


def paper_protective_order(position: BacktestTrade) -> tuple[float, bool, str] | None:
    """A resting stop, independent of a pattern's close-confirmation flags."""
    policy = position.paper_protection
    if policy is None:
        return None
    reason = "trailing_stop" if policy["peak_net_pct"] > 0 else "stop_loss"
    return policy["stop"], False, reason


def check_paper_protection_only(
    position: BacktestTrade, candle: OHLCVCandle, bar_idx: int,
    *, resting_candle: OHLCVCandle | None = None,
) -> tuple[float | None, str]:
    """Keep risk exits executable when a saved pattern engine is unavailable.

    Do not reinterpret an incompatible pattern's target, trail or time stop.
    An already queued market exit still executes at the following open.
    """
    from core.backtester import _gap_aware_trigger_fill

    order = paper_protective_order(position)
    resting_bar = resting_candle if resting_candle is not None else candle
    if order is None:
        raise ValueError("Independent paper protection is required")
    if position.pending_exit_reason:
        position.exit_bar_idx = bar_idx
        return resting_bar.open, position.pending_exit_reason
    stop, _, reason = order
    price = _gap_aware_trigger_fill(resting_bar, stop, is_short=position.action == "SELL", favorable=False)
    if price is not None:
        position.exit_bar_idx = bar_idx
        return price, reason
    ratchet_paper_protection(position, candle.close)
    return None, ""


def ratchet_paper_protection(position: BacktestTrade, close: float) -> None:
    """Tighten only AFTER checking the old order at this observed price.

    A completed close protects the next bar. A live quote protects later
    observations, with its timestamp/evidence retained by PaperAccount.
    """
    policy = position.paper_protection
    if policy is None:
        return
    policy["peak_net_pct"] = max(policy["peak_net_pct"], _net_return(position, close))
    floor = max(-policy["loss_limit_pct"], policy["peak_net_pct"] - policy["giveback_pct"])
    candidate = _trigger(position, floor)
    policy["stop"] = (min(policy["stop"], candidate) if position.action == "SELL"
                      else max(policy["stop"], candidate))
