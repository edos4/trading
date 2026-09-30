"""Pennant geometry must describe the setup the detector actually accepted."""
import importlib
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from patterns import _dedup
from patterns._annotations import pattern_annotations
from patterns._rationale import pattern_reasons
from analysis.chart_renderer import build_trade_viewer_payload

pennant = importlib.import_module('patterns.010_pennant')


def setup():
    df = pd.DataFrame(dict(open=100., high=101., low=99., close=100., volume=1000.),
                      index=pd.bdate_range('2026-01-01', periods=63))
    for col in ('open', 'close'):
        df.loc[df.index[50:55], col] = np.linspace(100, 120, 5)
    df.loc[df.index[50:55], 'high'] = np.linspace(101, 121, 5)
    df.loc[df.index[50:55], 'low'] = np.linspace(99, 119, 5)
    df.loc[df.index[50:55], 'volume'] = 2000
    df.loc[df.index[55:62], 'high'] = np.linspace(121, 120, 7)
    df.loc[df.index[55:62], 'low'] = np.linspace(117, 119, 7)
    df.loc[df.index[55:62], ['open', 'close']] = 119.5
    df.loc[df.index[55:62], 'volume'] = 500
    df.loc[df.index[62]] = [121, 123, 121, 122, 2000]
    return df


def detect(df, monkeypatch):
    # Isolate the window being checked rather than accepting a sibling setup.
    monkeypatch.setattr(pennant, 'FLAG_MIN_LEN', 5)
    monkeypatch.setattr(pennant, 'FLAG_MAX_LEN', 5)
    monkeypatch.setattr(pennant, 'CONSOL_MIN_LEN', 7)
    monkeypatch.setattr(pennant, 'CONSOL_MAX_LEN', 7)
    _dedup.reset()
    return pennant.PennantPattern().analyze(
        SimpleNamespace(symbol='TEST', timeframe='1d'),
        SimpleNamespace(get_df=lambda *a, **kw: df))


@pytest.mark.parametrize('bear', [False, True])
def test_valid_pennant_preserves_geometry_and_measured_reasons(monkeypatch, bear):
    df = setup()
    if bear:
        df[['open', 'high', 'low', 'close']] = 250 - df[['open', 'high', 'low', 'close']]
        df[['high', 'low']] = df[['low', 'high']].to_numpy()
    signal = detect(df, monkeypatch)
    assert signal is not None and signal.action == ('SELL' if bear else 'BUY')
    anns = signal.chart_annotations
    pole = next(a for a in anns if a['type'] == 'segment' and not a['label'])
    assert pole['start_price'] == df.close.iloc[50]
    assert pole['end_price'] == df.close.iloc[54]
    assert pole['start_price'] != pole['end_price']
    rails = [a for a in anns if 'pennant fit' in a.get('label', '')]
    upper, lower = [np.linspace(a['start_price'], a['end_price'], 7) for a in rails]
    assert np.all((df.close.iloc[55:62] >= lower) & (df.close.iloc[55:62] <= upper))
    assert all(a.get('reason') for a in anns)
    assert '2.0x volume' in pole['reason']
    # Replay must not rewrite saved measurements using revised vendor history.
    revised = df.copy()
    revised.loc[:, 'close'] = 999
    revised.loc[:, 'volume'] = 1
    upgraded = pattern_annotations(anns, revised, signal.pattern)
    assert [a['reason'] for a in upgraded] == [a['reason'] for a in anns]
    payload = build_trade_viewer_payload(revised, symbol='TEST', pattern=signal.pattern,
                                        annotations=anns)
    drawn = next(a for a in payload['segments'] if a['label'] == 'Pole')
    assert drawn['data'][0]['value'] == pole['start_price']
    assert drawn['data'][-1]['value'] == pole['end_price']
    assert drawn['reason'] == pole['reason']


@pytest.mark.parametrize('problem', ['flat_pole', 'wedge', 'negative_retrace',
                                     'outside_rail', 'crossed_rails', 'volume'])
def test_rejects_invalid_pennant(monkeypatch, problem):
    df = setup()
    coil = df.index[55:62]
    if problem == 'flat_pole':
        # The previous close must not supply the entire impulse.
        df.loc[df.index[50:55], ['open', 'close']] = 120
    elif problem == 'wedge':
        df.loc[coil, 'high'] = np.linspace(119, 121, 7)
        df.loc[coil, 'low'] = np.linspace(115, 120, 7)
        df.loc[coil, 'close'] = (df.loc[coil, 'high'] + df.loc[coil, 'low']) / 2
    elif problem == 'negative_retrace':
        df.loc[coil, ['open', 'high', 'low', 'close']] += 5
        df.loc[df.index[-1], ['open', 'high', 'low', 'close']] += 5
    elif problem == 'outside_rail':
        df.loc[coil[3], ['high', 'close']] = 121.5
    elif problem == 'crossed_rails':
        # Positive candle ranges can still produce crossing regression fits.
        df.loc[coil, 'high'] = [123, 123, 120, 120, 120, 120, 120]
        df.loc[coil, 'low'] = [117, 117, 119.9, 119.9, 119.9, 119.9, 119.9]
        df.loc[coil, 'close'] = 119.95
        df.loc[df.index[-1], ['high', 'close']] = 124
    elif problem == 'volume':
        df.loc[coil, 'volume'] = 1600
        df.loc[df.index[-1], 'volume'] = 4000
    assert detect(df, monkeypatch) is None


def test_pole_volume_uses_the_immediately_preceding_twenty_bars(monkeypatch):
    df = setup()
    df.loc[df.index[49], 'volume'] = 50000
    # Omitting the immediately preceding bar incorrectly passes the volume gate.
    assert detect(df, monkeypatch) is None


def test_legacy_pole_explanation_uses_saved_endpoints(monkeypatch):
    df = setup()
    signal = detect(df, monkeypatch)
    for ann in signal.chart_annotations:
        ann.pop('reason', None)
        if ann.get('label') == 'pole start':
            ann['price'] = 120.0
    notes = pattern_reasons(signal.pattern, signal.chart_annotations, df)
    assert 'pole +0%' in notes['Pole']
    assert 'starts at 120' in notes['pole start']
