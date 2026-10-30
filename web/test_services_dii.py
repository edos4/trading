"""Explorer applies the pattern gate at each detection, not the chart's last bar."""
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal
from web.services import ExplorerService


def _candles(close):
    return [OHLCVCandle(c, c+1, c-1, c, 1000, t.to_pydatetime())
            for c, t in zip(close, pd.bdate_range('2024-01-01', periods=len(close)))]


def _run(monkeypatch, patterns_v2, candles, action, index):
    sig = TradeSignal(symbol='TEST', action=action, pattern='double_bottom', timeframe='1d',
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
    return bool(result['signals'])


@pytest.mark.parametrize('patterns_v2', [False, True])
@pytest.mark.parametrize('action,index,accepted', [
    ('BUY', 99, True), ('SELL', 99, True),
    ('BUY', 129, False), ('SELL', 129, False),
])
def test_explorer_dii_uses_signal_prefix(monkeypatch, patterns_v2, action, index, accepted):
    close = np.concatenate([np.full(100, 100.), np.arange(101., 131.)])
    candles = _candles(close)
    assert _run(monkeypatch, patterns_v2, candles, action, index) == accepted


@pytest.mark.parametrize('action', ['BUY', 'SELL'])
def test_explorer_dii_band_rejects_extremes_for_both_actions(monkeypatch, action):
    close = np.concatenate([np.full(100, 500.), 500. - np.arange(1., 31.)])
    candles = _candles(close)
    assert _run(monkeypatch, False, candles, action, 129) is False


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
