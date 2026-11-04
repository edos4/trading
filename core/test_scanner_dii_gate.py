import asyncio
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from config import settings
from core.scanner import MarketScanner
from patterns.base_pattern import TradeSignal


def _frame_at(bulls: float, n: int = 100) -> pd.DataFrame:
    """A flat frame whose trailing 40 bars produce the intended bulls
    percentage at the last bar. The engine's rolling-window counts are
    position-sensitive (tied highs/lows saturate for the flat prefix
    bars), so the helper places unique low-nudges at evenly spread
    positions in the trailing 40 bars. Each nudge is a unique sub-bar
    drop (0.001 * (j+1)) so its low is a strict window minimum, while
    the flat baseline highs stay above every nudged high so up moves
    still count as window maxima. With evenly spread nudges the engine
    counts each nudge as exactly one down event, giving bulls ≈ 50
    - 2.5 * n_down for n_down >= 2 (the first two nudges interfere with
    the rolling-minimum warmup). n_down = 0 gives 50.0.
    Callers needing n_down < 2 for a target below 50 should pass a
    target of at least 50 (the engine's minimum granularity for this
    frame shape is 2.5 steps)."""
    n_down = int(round((100 - bulls) / 2.5))
    close = pd.Series(np.full(n, 100.0))
    start = n - 40
    if n_down > 0:
        for j in range(n_down):
            i = start + (j * 40 // n_down)
            close.iloc[i] = 100.0 - (0.001 * (j + 1))
    high = (close + 1.0).copy()
    low = (close - 1.0).copy()
    return pd.DataFrame(dict(open=close.values, high=high.values, low=low.values, close=close.values, volume=1000))


@pytest.mark.parametrize('patterns_v2', [False, True])
@pytest.mark.parametrize('direction,short,action,accepted', [
    ('flat', False, 'BUY', True),
    ('flat', False, 'SELL', True),
    ('up', False, 'BUY', False),
    ('up', False, 'SELL', False),
    ('down', False, 'BUY', False),
    ('down', False, 'SELL', False),
    ('flat', True, 'BUY', True),  # short warmup window: DII gate is skipped
])
def test_both_pattern_scanners_gate_before_other_entry_checks(
        monkeypatch, patterns_v2, direction, short, action, accepted):
    monkeypatch.setattr(settings, "short_entries_enabled", True)
    scanner = MarketScanner(symbols=['TEST'], market='us', patterns_v2=patterns_v2, volume_gate=False)
    if direction == 'flat':
        if action == 'BUY':
            # 40% bulls sits inside the BUY band (bulls 17–40).
            frame = _frame_at(40.0, n=120)
        else:
            # 60% bulls → 40% bears sits inside the SELL band (bears 21–42).
            frame = _frame_at(60.0, n=120)
    else:
        frame = _frame_at(0.0, n=120)
    scanner._store.get_df = Mock(return_value=frame)
    scanner._append_signal_log = Mock()
    scanner._finish_signal = AsyncMock()
    scanner._pattern_only = True
    monkeypatch.setattr('core.scanner.regime_filter_required', lambda *a: False)
    monkeypatch.setattr('core.scanner.describe_risk_gate_rejection', lambda *a, **kw: None)
    signal = TradeSignal(symbol='TEST', action=action, pattern='double_bottom' if patterns_v2 else 'pattern_003_double_bottom', timeframe='1d', confidence=1, price=100, qty=1, stop_loss=95, take_profit=110)
    asyncio.run(scanner._process_signal(signal))
    if accepted:
        scanner._finish_signal.assert_awaited_once()
    else:
        scanner._finish_signal.assert_not_awaited()
        assert scanner._append_signal_log.call_args.kwargs['status'] == 'rejected'
        label = 'bullish' if action == 'BUY' else 'bearish'
        assert f'DII {label} percentage' in scanner._append_signal_log.call_args.kwargs['reason']
        assert scanner.stats['dii_rejected'] == 1


def test_indicators_only_bypasses_pattern_gate(monkeypatch):
    scanner = MarketScanner(symbols=['TEST'], market='us', indicators_only=True, volume_gate=False)
    scanner._append_signal_log = Mock()
    # A later rejection proves DII was bypassed even without any history.
    signal = TradeSignal(symbol='TEST', action='BUY', pattern='indicator_directional_imbalance_index', timeframe='1d', confidence=1, price=100, qty=1, blocked_reason='later gate')
    asyncio.run(scanner._process_signal(signal))
    assert scanner._append_signal_log.call_args.kwargs['reason'] == 'later gate'
    assert not scanner.stats.get('dii_rejected')
