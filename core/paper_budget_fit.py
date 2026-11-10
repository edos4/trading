"""Causal volatility fit of a Patternsv2 entry to its initial paper budget."""
from __future__ import annotations

import math
from datetime import date

from patterns.base_pattern import TradeSignal


def budget_fit(signal: TradeSignal, geometry: dict, minimum: float) -> dict:
    """Record a prior-ATR ratio; optionally veto before any fee or position.

    The execution candle's high, low and close are deliberately not inputs.
    Missing decision-time evidence can never pass an enabled minimum.
    """
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError('Minimum paper stop ATR must be finite and nonnegative')
    evidence = signal.pattern_evidence or {}
    atr = evidence.get('atr_price')
    valid = (not isinstance(atr, bool) and isinstance(atr, (int, float))
             and math.isfinite(atr) and atr > 0)
    try:
        as_of = date.fromisoformat(evidence['atr_as_of'])
        valid = valid and signal.signal_bar_timestamp is not None and as_of == signal.signal_bar_timestamp.date()
    except (KeyError, TypeError, ValueError):
        valid = False
    distance = geometry['paper_initial_distance']
    ratio = distance/atr if valid and distance is not None else None
    result = {'policy': 'prior-atr-budget-fit-v1', 'atr_price': atr if valid else None,
              'atr_as_of': evidence.get('atr_as_of'), 'initial_stop_atr': ratio,
              'minimum_stop_atr': minimum, 'mode': 'filter' if minimum > 0 else 'shadow',
              'verified': valid, 'passed': ratio is not None and ratio >= minimum}
    if minimum > 0 and not valid:
        raise ValueError('Paper V2 budget fit requires saved decision-time ATR')
    if minimum > 0 and not result['passed']:
        raise ValueError(f'Paper V2 budget fit: initial stop is {ratio:.3f} ATR, below {minimum:g} ATR')
    return result
