"""Explorer applies the pattern gate at each detection, not the chart's last bar."""
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal
from web.services import ExplorerService


@pytest.mark.parametrize('patterns_v2', [False, True])
@pytest.mark.parametrize('index,accepted', [(99, True), (129, False)])
def test_explorer_dii_uses_signal_prefix(monkeypatch, patterns_v2, index, accepted):
    close = np.concatenate([np.full(100, 100.), np.arange(101., 131.)])
    candles = [OHLCVCandle(c, c+1, c-1, c, 1000, t.to_pydatetime())
               for c, t in zip(close, pd.bdate_range('2024-01-01', periods=len(close)))]
    sig = TradeSignal(symbol='TEST', action='BUY', pattern='double_bottom', timeframe='1d',
                      confidence=1, price=100, qty=1, signal_bar_idx=index)
    monkeypatch.setattr('web.services.fetch_ohlcv_candles', lambda *a, **kw: candles)
    monkeypatch.setattr('web.services.detect_trade_signals', lambda *a, **kw: [sig])
    monkeypatch.setattr('web.services.latest_signals_over_lookback', lambda *a, **kw: [sig])
    monkeypatch.setattr('web.services.passes_min_confidence', lambda *a: True)
    monkeypatch.setattr('web.services.passes_regime_filter', lambda *a: True)
    service = ExplorerService()
    service._patterns = []
    service._renderer.render = Mock(return_value=b'png')
    result = service.load_symbol('TEST', 'NASDAQ', '1d', patterns_v2=patterns_v2, volume_gate=False)
    assert bool(result['signals']) == accepted


def test_chart_scan_retains_the_detected_bar_index():
    from patterns.chart_scan import latest_signals_over_lookback
    candles = [OHLCVCandle(100, 101, 99, 100, 1000, t.to_pydatetime())
               for t in pd.bdate_range('2024-01-01', periods=120)]
    class Pattern:
        name = 'test'
        timeframes = ['1d']
        MIN_BARS = 80
        def analyze(self, snapshot, store):
            if store.available('TEST', '1d') != 100: return None
            return TradeSignal(symbol='TEST', action='BUY', pattern='test', timeframe='1d', confidence=1, price=100, qty=1)
    result = latest_signals_over_lookback([Pattern()], 'TEST', '1d', candles, session_tz='America/New_York')
    assert result[0].signal_bar_idx == 99
    assert result[0].signal_bar_timestamp == candles[99].timestamp
