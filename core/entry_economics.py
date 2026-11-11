"""Opening-price target economics using observed quotes and execution costs."""
from __future__ import annotations

import math


def net_direction_return(price: float, future: float, action: str,
                         fee: float, slippage: float) -> float:
    """Price-direction return after both fees and adverse exit slippage.

    Negative values favour a short; positive values favour a long. ``price``
    already contains entry slippage, so it is never charged twice.
    """
    short = action == 'SELL'
    exit_price = future * (1 + slippage if short else 1 - slippage)
    friction = fee * (price + exit_price) / price
    return (exit_price - price) / price + (friction if short else -friction)


def validate_target_reward(signal, price: float, fee: float, slippage: float) -> None:
    """A successful take-profit must pay its own execution costs."""
    if signal.take_profit is None:
        return
    value = net_direction_return(price, signal.take_profit, signal.action, fee, slippage)
    reward = -value if signal.action == 'SELL' else value
    if not math.isfinite(reward) or reward <= 0:
        raise ValueError('Execution economics: target has no positive net reward after costs')
