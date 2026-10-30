"""Algorithm verification against an independent loop, not TradingView parity."""
import numpy as np
import pandas as pd
import pytest

from analysis.indicator_engine import IndicatorEngine


def frame(high, low):
    return pd.DataFrame(dict(open=high, high=high, low=low, close=high, volume=1.0))


def oracle(df, length, period):
    out = {key: np.full(len(df), np.nan) for key in ('bulls', 'bears', 'up', 'down', 'upper', 'lower')}
    events = []
    for t in range(len(df)):
        h = df.high.iloc[max(0, t-length):t+1].to_numpy(float)
        l = df.low.iloc[max(0, t-length):t+1].to_numpy(float)
        if len(h) == length+1:
            if np.isfinite(h).all(): out['upper'][t] = max(h)
            if np.isfinite(l).all(): out['lower'][t] = min(l)
        valid = np.isfinite(out['upper'][t]) and np.isfinite(out['lower'][t])
        events.append((float(h[-1] == max(h)), float(l[-1] == min(l))) if valid else (np.nan, np.nan))
        window = events[max(0, t-period+1):t+1]
        if len(window) == period and np.isfinite(window).all():
            up, down = map(sum, zip(*window))
            out['up'][t], out['down'][t] = up, down
            if up+down:
                out['bulls'][t], out['bears'][t] = 100*up/(up+down), 100*down/(up+down)
    return out


@pytest.mark.parametrize('length,period', [(60,40), (1,1), (3,7)])
@pytest.mark.parametrize('kind', ['rising', 'falling', 'flat', 'contracting', 'ties', 'invalid', 'empty', 'short'])
def test_loop_oracle_and_causality(length, period, kind):
    rng = np.random.default_rng(44)
    h = rng.integers(10, 20, 180).astype(float)
    l = rng.integers(0, 10, 180).astype(float)
    if kind == 'rising': h, l = np.arange(180.)+10, np.arange(180.)
    if kind == 'falling': h, l = 200-np.arange(180.), 190-np.arange(180.)
    if kind == 'flat': h, l = np.full(180, 10.), np.full(180, 1.)
    if kind == 'contracting': h, l = 500-np.arange(180.), np.arange(180.)
    if kind == 'invalid': h[90], l[120] = np.inf, np.nan
    if kind == 'empty': h, l = [], []
    if kind == 'short': h, l = h[:1], l[:1]
    df = frame(h, l)
    df.index = pd.RangeIndex(10, 10+len(df))
    original = df.copy(deep=True)
    actual = IndicatorEngine(df).directional_imbalance_index(length, period)
    expected = oracle(df, length, period)
    assert list(actual) == list(expected)
    for key, series in actual.items():
        assert series.index.equals(pd.RangeIndex(len(df)))
        np.testing.assert_allclose(series, expected[key], equal_nan=True)
        prefix = IndicatorEngine(df.iloc[:100]).directional_imbalance_index(length, period)[key]
        np.testing.assert_allclose(series.iloc[:100], prefix, equal_nan=True)
        if len(df) >= length+period:
            truncated = IndicatorEngine(df.iloc[-(length+period):]).directional_imbalance_index(length, period)[key]
            np.testing.assert_allclose(series.iloc[-1], truncated.iloc[-1], equal_nan=True)
    pd.testing.assert_frame_equal(df, original)


def test_default_startup_and_zero_total():
    rising = IndicatorEngine(frame(np.arange(100.)+10, np.arange(100.))).directional_imbalance_index()
    assert rising['bulls'].first_valid_index() == 59
    assert rising['up'].iloc[59] == 40
    assert rising['bulls'].iloc[59] == 100
    contracting = IndicatorEngine(frame(500-np.arange(100.), np.arange(100.))).directional_imbalance_index()
    assert contracting['up'].iloc[-1] == contracting['down'].iloc[-1] == 0
    assert np.isnan(contracting['bulls'].iloc[-1])


@pytest.mark.parametrize('bad', [True, False, 0, -1, 1.0, 1.5, '10', None, np.bool_(True)])
@pytest.mark.parametrize('arg', ['length', 'period'])
def test_invalid_parameters(bad, arg):
    with pytest.raises(ValueError, match=f'{arg} must be a positive integer'):
        IndicatorEngine(frame([], [])).directional_imbalance_index(**{arg: bad})
