import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from analysis.test_pattern_dii import frame
from core.scanner import MarketScanner
from patterns.base_pattern import TradeSignal


@pytest.mark.parametrize('patterns_v2', [False, True])
@pytest.mark.parametrize('rising,short,accepted', [(False, False, True), (True, False, False), (False, True, False)])
def test_both_pattern_scanners_gate_before_other_entry_checks(monkeypatch, patterns_v2, rising, short, accepted):
    scanner = MarketScanner(symbols=['TEST'], market='us', patterns_v2=patterns_v2, volume_gate=False)
    scanner._store.get_df = Mock(return_value=frame(30 if short else 100, rising=rising))
    scanner._append_signal_log = Mock()
    scanner._finish_signal = AsyncMock()
    scanner._min_share_price = 0
    scanner._pattern_only = True
    monkeypatch.setattr('core.scanner.regime_filter_required', lambda *a: False)
    monkeypatch.setattr('core.scanner.describe_risk_gate_rejection', lambda *a, **kw: None)
    signal = TradeSignal(symbol='TEST', action='BUY', pattern='double_bottom' if patterns_v2 else 'pattern_003_double_bottom', timeframe='1d', confidence=1, price=100, qty=1, stop_loss=95, take_profit=110)
    asyncio.run(scanner._process_signal(signal))
    if accepted:
        scanner._finish_signal.assert_awaited_once()
    else:
        scanner._finish_signal.assert_not_awaited()
        assert scanner._append_signal_log.call_args.kwargs['status'] == 'rejected'
        assert 'DII bullish percentage' in scanner._append_signal_log.call_args.kwargs['reason']
        assert scanner.stats['dii_rejected'] == 1


def test_indicators_only_bypasses_pattern_gate(monkeypatch):
    scanner = MarketScanner(symbols=['TEST'], market='us', indicators_only=True, volume_gate=False)
    scanner._append_signal_log = Mock()
    # A later rejection proves DII was bypassed even without any history.
    signal = TradeSignal(symbol='TEST', action='BUY', pattern='indicator_directional_imbalance_index', timeframe='1d', confidence=1, price=100, qty=1, blocked_reason='later gate')
    asyncio.run(scanner._process_signal(signal))
    assert scanner._append_signal_log.call_args.kwargs['reason'] == 'later gate'
    assert not scanner.stats.get('dii_rejected')
