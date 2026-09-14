"""Trusted harness copied into isolated inputs; never launch directly on a draft."""
import contextlib
from dataclasses import asdict
from datetime import datetime
import importlib
import io
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def evaluate(request):
    from data.ohlcv_store import OHLCVStore
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import BasePattern, TradeSignal
    from patterns.chart_scan import latest_signals_over_lookback, _snapshot
    from patterns import _dedup
    _dedup.reset()
    if request.get('operation') == 'tests':
        import pytest
        result = pytest.main(['-q','-p','no:cacheprovider',*request['test_paths']])
        return {'tests_passed':result==0,'exit_code':int(result)}
    module = importlib.import_module(request['source_path'][:-3].replace('/','.'))
    classes = [c for c in vars(module).values() if isinstance(c,type) and c is not BasePattern
               and issubclass(c,BasePattern) and c.__module__ == module.__name__]
    if len(classes)!=1:
        raise ValueError('Expected one detector class')
    pattern = classes[0]()
    if pattern.name != request['pattern_id'] or pattern.skipped:
        raise ValueError('Pattern identity/availability changed')
    candles = [OHLCVCandle(open=c['open'],high=c['high'],low=c['low'],close=c['close'],volume=c['volume'],
                          timestamp=datetime.fromisoformat(c['timestamp'])) for c in request['candles']]
    if request.get('operation') == 'analyze':
        store = OHLCVStore(window=max(len(candles),512),session_tz=request['session_timezone'])
        store.replace_all(request['symbol'],request['timeframe'],candles)
        current = request.get('current_bar')
        if current is not None:
            _dedup.set_current(current)
        _dedup.mark(*request.get('used_pivots',[]))
        signal = pattern.analyze(_snapshot(request['symbol'],request['timeframe'],candles[current if current is not None else -1]),store)
        signals = [signal] if signal else []
    else:
        signals = latest_signals_over_lookback([pattern],request['symbol'],request['timeframe'],candles,
                                              lookback=request.get('lookback',30),session_tz=request['session_timezone'])
    for sig in signals:
        if not isinstance(sig,TradeSignal) or sig.pattern != pattern.name:
            raise ValueError('Incompatible signal')
    return {'signals':[asdict(s) for s in signals], 'metadata':{
        'name':pattern.name,'timeframes':pattern.timeframes,'MIN_BARS':getattr(pattern,'MIN_BARS',2),
        'HORIZON_BARS':getattr(pattern,'HORIZON_BARS',5),
        'MAX_OPEN_PER_SYMBOL':getattr(pattern,'MAX_OPEN_PER_SYMBOL',None)}}


if __name__ == '__main__':
    with contextlib.redirect_stdout(io.StringIO()):
        result = evaluate(json.loads(Path('/input/request.json').read_text()))
    print(json.dumps(result,default=str,allow_nan=False))
