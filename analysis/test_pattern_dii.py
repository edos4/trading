import numpy as np
import pandas as pd
import pytest

from analysis.pattern_dii import describe_pattern_dii_rejection


def frame(n=100, rising=False, falling=False):
    if rising:
        close = np.arange(n, dtype=float) + 100
    elif falling:
        close = 500. - np.arange(n, dtype=float)
    else:
        close = np.full(n, 100.)
    return pd.DataFrame(dict(open=close, high=close+1, low=close-1, close=close, volume=1000))


@pytest.mark.parametrize('bulls,accepted', [
    (40, True), (50, True), (60, True),
    (39.999, False), (60.001, False), (0, False), (100, False), (np.nan, False),
])
def test_inclusive_consideration_band(monkeypatch, bulls, accepted):
    monkeypatch.setattr(
        'analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
        lambda self, *args: {'bulls': pd.Series([bulls]),
                             'bears': pd.Series([100 - bulls if np.isfinite(bulls) else np.nan]),
                             'up': pd.Series([1.0]), 'down': pd.Series([1.0])})
    assert (describe_pattern_dii_rejection(frame()) is None) == accepted


def test_actual_calculation_and_unavailable():
    assert describe_pattern_dii_rejection(frame()) is None
    assert '100.00%' in describe_pattern_dii_rejection(frame(rising=True))
    assert '0.00%' in describe_pattern_dii_rejection(frame(falling=True))
    assert describe_pattern_dii_rejection(frame(59)) is None  # warmup window: skipped
    assert describe_pattern_dii_rejection(None) is None
    invalid = frame()
    invalid.loc[90, 'high'] = np.inf
    assert 'unavailable' in describe_pattern_dii_rejection(invalid)
    contracting = frame()
    contracting['high'], contracting['low'] = 500-np.arange(100.), np.arange(100.)
    assert describe_pattern_dii_rejection(contracting) is None  # zero events: balanced


def test_zero_event_window_is_balanced(monkeypatch):
    nan = pd.Series([np.nan])
    zero = pd.Series([0.0])
    monkeypatch.setattr(
        'analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
        lambda self, *args: {'bulls': nan, 'bears': nan, 'up': zero, 'down': zero})
    assert describe_pattern_dii_rejection(frame()) is None
