import numpy as np
import pandas as pd
import pytest

from analysis.pattern_dii import describe_pattern_dii_rejection


def _frame(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(dict(open=close, high=close + 1, low=close - 1,
                             close=close, volume=1000))


def frame(n=100, rising=False, falling=False):
    if rising:
        close = np.arange(n, dtype=float) + 100
    elif falling:
        close = 500. - np.arange(n, dtype=float)
    else:
        close = np.full(n, 100.)
    return _frame(close)


def _state(bulls, bears):
    return {'bulls': pd.Series([bulls]), 'bears': pd.Series([bears]),
            'up': pd.Series([1.0]), 'down': pd.Series([1.0])}


@pytest.mark.parametrize('action,value,accepted', [
    ('BUY', 17, True), ('BUY', 20, True), ('BUY', 30, True),
    ('BUY', 40, True),
    ('BUY', 16.999, False), ('BUY', 16, False), ('BUY', 40.001, False), ('BUY', 45, False),
    ('BUY', 0, False),
    ('BUY', 50, False), ('BUY', 60, False), ('BUY', 100, False),
    ('BUY', np.nan, False),
    ('SELL', 26, True), ('SELL', 27, True), ('SELL', 30, True),
    ('SELL', 40, True), ('SELL', 42, True),
    ('SELL', 25.999, False), ('SELL', 25, False), ('SELL', 23, False),
    ('SELL', 22, False), ('SELL', 21, False), ('SELL', 16, False),
    ('SELL', 42.001, False), ('SELL', 45, False),
    ('SELL', 0, False), ('SELL', 50, False), ('SELL', 60, False),
    ('SELL', 100, False), ('SELL', np.nan, False),
])
def test_inclusive_side_band(monkeypatch, action, value, accepted):
    # BUY reads the bullish percentage; SELL reads the bearish percentage.
    finite = np.isfinite(value)
    if action == 'BUY':
        bulls, bears = value, (100 - value if finite else np.nan)
    else:
        bulls, bears = (100 - value if finite else np.nan), value
    monkeypatch.setattr(
        'analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
        lambda self, *args: _state(bulls, bears))
    reason = describe_pattern_dii_rejection(frame(), action=action)
    if accepted:
        assert reason is None
    elif finite:
        assert reason is not None
        assert f"{value:.2f}%" in reason
        assert action in reason
        assert ('bullish' if action == 'BUY' else 'bearish') in reason
    else:
        assert 'unavailable' in reason


def test_bands_do_not_overlap(monkeypatch):
    """40/60 passes only BUY, 58/42 only SELL, and the 40–58 middle passes neither."""

    def reason_for(bulls, bears, action):
        monkeypatch.setattr(
            'analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
            lambda self, *args: _state(bulls, bears))
        return describe_pattern_dii_rejection(frame(), action=action)

    assert reason_for(40, 60, 'BUY') is None
    assert reason_for(40, 60, 'SELL') is not None
    assert reason_for(45, 55, 'BUY') is not None
    assert reason_for(45, 55, 'SELL') is not None
    assert reason_for(58, 42, 'SELL') is None
    assert reason_for(58, 42, 'BUY') is not None
    assert reason_for(79, 21, 'SELL') is not None
    assert reason_for(79, 21, 'BUY') is not None
    assert reason_for(77, 23, 'SELL') is not None
    assert reason_for(77, 23, 'BUY') is not None
    assert reason_for(75, 25, 'SELL') is not None
    assert reason_for(75, 25, 'BUY') is not None
    assert reason_for(74, 26, 'SELL') is None
    assert reason_for(74, 26, 'BUY') is not None
    assert reason_for(78, 22, 'SELL') is not None
    assert reason_for(78, 22, 'BUY') is not None
    assert reason_for(55, 45, 'BUY') is not None
    assert reason_for(55, 45, 'SELL') is not None
    assert reason_for(50, 50, 'BUY') is not None
    assert reason_for(50, 50, 'SELL') is not None


def test_actual_calculation_and_unavailable():
    rising = frame(rising=True)
    falling = frame(falling=True)
    assert describe_pattern_dii_rejection(rising, action="BUY") is not None
    assert '100.00%' in describe_pattern_dii_rejection(rising, action="BUY")
    assert describe_pattern_dii_rejection(falling, action="BUY") is not None
    assert '0.00%' in describe_pattern_dii_rejection(falling, action="BUY")
    assert describe_pattern_dii_rejection(falling, action="SELL") is not None
    assert '100.00%' in describe_pattern_dii_rejection(falling, action="SELL")
    assert 'bearish' in describe_pattern_dii_rejection(falling, action="SELL")

    bullish_tape = _frame(100 + np.cumsum(np.random.default_rng(7).normal(0.05, 1, 120)))
    assert describe_pattern_dii_rejection(bullish_tape, action="SELL") is None
    assert describe_pattern_dii_rejection(bullish_tape, action="BUY") is not None

    assert describe_pattern_dii_rejection(frame(59)) is None  # warmup window: skipped
    assert describe_pattern_dii_rejection(None) is None
    invalid = frame()
    invalid.loc[90, 'high'] = np.inf
    assert 'unavailable' in describe_pattern_dii_rejection(invalid)
    contracting = frame()
    contracting['high'], contracting['low'] = 500 - np.arange(100.), np.arange(100.)
    assert describe_pattern_dii_rejection(contracting) == (
        'DII bullish percentage 50.00% outside required 17%–40% for BUY (20, 40).')


def test_zero_event_window_is_balanced(monkeypatch):
    zero = pd.Series([0.0])
    monkeypatch.setattr(
        'analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
        lambda self, *args: {'bulls': pd.Series([np.nan]), 'bears': pd.Series([np.nan]),
                             'up': zero, 'down': zero})
    assert describe_pattern_dii_rejection(frame(), action="BUY") == (
        'DII bullish percentage 50.00% outside required 17%–40% for BUY (20, 40).')
    assert describe_pattern_dii_rejection(frame(), action="SELL") == (
        'DII bearish percentage 50.00% outside required 26%–42% for SELL (20, 40).')
