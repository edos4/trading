"""Trusted P0 expectations: no generated candidate code or provider needed."""
import hashlib
import importlib
import json
from pathlib import Path

import pandas as pd
import pytest

from analysis.indicator_engine import IndicatorEngine
from core.backtester import _check_exit, _open_trade
from core.paper_trader import PaperAccount, _trade_from_dict, _trade_to_dict
from data.tv_client import OHLCVCandle
from patterns import _dedup
from patterns.base_pattern import TradeSignal
from patterns.chart_scan import latest_signals_over_lookback

FIXTURES = Path(__file__).resolve().parents[1] / 'tests/fixtures/pattern_edit'


def frame(name):
    return pd.read_csv(FIXTURES / f'{name}.csv', index_col='date', parse_dates=True)


def candles(name):
    return [OHLCVCandle(**row.to_dict(), timestamp=ts.to_pydatetime())
            for ts, row in frame(name).iterrows()]


def test_frozen_fixture_hashes():
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    for filename, expected in manifest['files'].items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize('name,rs,entry', [('hs_control', 210, 216), ('hs_correction', 197, 204)])
def test_inspected_hs_geometry(name, rs, entry):
    pattern = importlib.import_module('patterns.008_head_and_shoulders').HeadAndShouldersPattern()
    ind = IndicatorEngine(frame(name))
    setup = pattern._evaluate(ind, ind.rsi_wilder(14), 150, entry)
    assert setup is not None
    assert (setup.left_shoulder, setup.left_neck, setup.head, setup.right_neck) == (100, 120, 150, 180)
    assert (setup.right_shoulder, setup.entry, setup.neckline, setup.target) == (rs, entry, 90, 60)
    # Requested later peak is a real, confirmed peak below the head/LS.
    assert ind.close.iloc[210] == 94.5


@pytest.mark.parametrize('market,tz', [('us', 'America/New_York'), ('ph', 'Asia/Manila')])
def test_no_pattern_on_flat_data(market, tz):
    pattern = importlib.import_module('patterns.008_head_and_shoulders').HeadAndShouldersPattern()
    _dedup.reset()
    try:
        assert latest_signals_over_lookback([pattern], 'FIXTURE', '1d', candles('no_pattern'), session_tz=tz) == []
    finally:
        _dedup.reset()


@pytest.mark.parametrize('market', ['us', 'ph'])
@pytest.mark.parametrize('candidate', [False, True])
def test_rule_fixture_and_legacy_paper_roundtrip(tmp_path, market, candidate):
    bars = candles('rule_change')
    entry = 1 if candidate else 0
    signal = TradeSignal(symbol='FIXTURE', action='BUY', pattern='fixture', timeframe='1d', confidence=1,
                         price=103 if candidate else 100, qty=1,
                         stop_loss=96 if candidate else 95, stop_loss_on_close=candidate, take_profit=112 if candidate else 110,
                         exit_bars_after_entry=1 if candidate else None)
    position = _open_trade(signal, bars[entry], entry)
    # Legacy record has no editor provenance. P2 must label it legacy_unknown.
    raw = _trade_to_dict(position)
    raw.pop('pattern_version_id', None)
    raw.pop('provenance', None)
    raw.pop('trade_id', None)
    account = PaperAccount(market=market, txn_cost_pct=0, slippage_pct=0)
    account.positions = {'FIXTURE': [_trade_from_dict(raw)]}
    path = tmp_path / f'{market}.json'
    account.save(path)
    restored = PaperAccount.load(path, market=market).positions['FIXTURE'][0]
    expected = (97, 'time_exit') if candidate else (95, 'stop_loss')
    for idx in range(entry + 1, len(bars)):
        direct = _check_exit(bars[idx], position, idx)
        assert _check_exit(bars[idx], restored, idx) == direct
        if direct[0] is not None:
            assert idx == (2 if candidate else 3)
            assert direct == expected
            break
    else:
        pytest.fail('Expected exit was not reached')


@pytest.mark.parametrize('name,count', [('hs_control', 1), ('hs_correction', 0)])
@pytest.mark.parametrize('tz', ['America/New_York', 'Asia/Manila'])
def test_causal_prefix_baselines(name, count, tz):
    pattern = importlib.import_module('patterns.008_head_and_shoulders').HeadAndShouldersPattern()
    _dedup.reset()
    try:
        signals = latest_signals_over_lookback([pattern], 'FIXTURE', '1d', candles(name), session_tz=tz)
        assert len(signals) == count
        if signals:
            assert signals[0].setup_key == (150, 210)
            assert signals[0].price == 88
            assert signals[0].stop_loss == 94.5
            assert signals[0].take_profit == 60
    finally:
        _dedup.reset()


def _bar(day, close, high=None, low=None):
    return OHLCVCandle(
        open=close, high=high if high is not None else close + 1,
        low=low if low is not None else close - 1, close=close, volume=1000,
        timestamp=pd.Timestamp(day, tz='America/New_York').to_pydatetime(),
    )


def _timer_position(**signal_kwargs):
    signal = TradeSignal(
        symbol='FIXTURE', action='BUY', pattern='fixture', timeframe='1d', confidence=1,
        price=100, qty=1, exit_bars_after_entry=1, time_exit_only_unfavorable=True,
        stop_loss=None, take_profit=None, **signal_kwargs,
    )
    return _open_trade(signal, _bar('2024-01-02', 100.0), 0)


def test_time_exit_only_unfavorable_suppresses_green_timer():
    # Q001/P2.4: a favorable close must not be cut by the bar timer.
    position = _timer_position()
    assert _check_exit(_bar('2024-01-03', 101.0), position, 1) == (None, '')
    # Once underwater the timer fires on the close.
    assert _check_exit(_bar('2024-01-04', 99.0), position, 2) == (99.0, 'time_exit')


def test_time_exit_min_mfe_floor_keeps_working_trades():
    # Never reached the 5% floor: the green zombie is given up at the timer.
    zombie = _timer_position(time_exit_min_mfe_pct=0.05)
    assert _check_exit(_bar('2024-01-03', 101.0, high=101.0), zombie, 1) == (101.0, 'time_exit')
    # Reached the floor earlier: the timer no longer cuts the winner.
    survivor = _timer_position(time_exit_min_mfe_pct=0.05)
    assert _check_exit(_bar('2024-01-03', 106.0, high=106.0), survivor, 1) == (None, '')
    assert _check_exit(_bar('2024-01-04', 101.0, high=101.0), survivor, 2) == (None, '')
