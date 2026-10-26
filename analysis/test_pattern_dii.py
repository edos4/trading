import numpy as np
import pandas as pd
import pytest

from analysis.pattern_dii import describe_pattern_dii_rejection


def frame(n=100, rising=False):
    close = np.arange(n, dtype=float) + 100 if rising else np.full(n, 100.)
    return pd.DataFrame(dict(open=close, high=close+1, low=close-1, close=close, volume=1000))


@pytest.mark.parametrize('bulls,accepted', [(45, True), (50, True), (55, True),
    (44.999, False), (55.001, False), (0, False), (100, False), (np.nan, False)])
def test_inclusive_range(monkeypatch, bulls, accepted):
    monkeypatch.setattr('analysis.pattern_dii.IndicatorEngine.directional_imbalance_index',
                        lambda self: {'bulls': pd.Series([bulls])})
    assert (describe_pattern_dii_rejection(frame()) is None) == accepted


def test_actual_calculation_and_unavailable():
    assert describe_pattern_dii_rejection(frame()) is None
    assert '100.00%' in describe_pattern_dii_rejection(frame(rising=True))
    assert describe_pattern_dii_rejection(frame(79))
    assert describe_pattern_dii_rejection(None)
    invalid = frame()
    invalid.loc[90, 'high'] = np.inf
    assert 'unavailable' in describe_pattern_dii_rejection(invalid)
    contracting = frame()
    contracting['high'], contracting['low'] = 500-np.arange(100.), np.arange(100.)
    assert 'unavailable' in describe_pattern_dii_rejection(contracting)
