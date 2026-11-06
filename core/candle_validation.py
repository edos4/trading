"""Validate observed execution candles without inventing missing prices."""
from __future__ import annotations

from datetime import datetime
import math


def candle_error(candle, *, require_timestamp: bool = True) -> str | None:
    """Return why an observation cannot support order execution or a mark."""
    try:
        prices = [getattr(candle, key) for key in ('open', 'high', 'low', 'close')]
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in prices):
            return 'prices must be finite and positive'
        o, h, l, c = prices
        if not l <= min(o, c) <= max(o, c) <= h:
            return 'OHLC range must contain open and close'
        volume = candle.volume
        if isinstance(volume, bool) or not math.isfinite(volume) or volume < 0:
            return 'volume must be finite and nonnegative'
        stamp = candle.timestamp
        if stamp is None and not require_timestamp:
            return None
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            return 'timestamp must be an aware datetime'
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 'malformed candle fields'
    return None
