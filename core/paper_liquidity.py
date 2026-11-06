"""Execution participation limits based only on completed pre-fill volume."""
from __future__ import annotations

from core.candle_validation import candle_error


def execution_liquidity(signal, store, max_participation: float, require_history: bool) -> dict:
    history=(store.copy_candles(signal.symbol,signal.timeframe)
             if store is not None and callable(getattr(store,'copy_candles',None)) else [])
    bars=[c for c in history
          if c.timestamp is not None and c.timestamp.tzinfo is not None
          and signal.signal_bar_timestamp is not None and c.timestamp<=signal.signal_bar_timestamp][-20:]
    if len(bars)<20 or any(candle_error(c) or c.volume<=0 for c in bars):
        if require_history:
            raise ValueError('Execution liquidity: require 20 valid dated volume observations')
        return {'verified':False,'reason':'20 valid dated volume observations unavailable'}
    volume=sum(c.volume for c in bars)/20
    participation=signal.qty/volume
    if max_participation>0 and participation>max_participation:
        raise ValueError(f'Execution liquidity: {participation:.2%} of prior average daily shares exceeds {max_participation:.2%}')
    return {'verified':True,'prior_average_daily_volume':volume,'participation':participation,
            'maximum_participation':max_participation,'as_of':signal.signal_bar_timestamp.isoformat()}
