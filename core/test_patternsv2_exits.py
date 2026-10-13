"""Patternsv2 resting orders use prior closes and executable OHLC fills."""
from datetime import datetime, timedelta, timezone

import pytest

from core.backtester import _check_exit, _open_trade
from core.paper_trader import PaperAccount, _trade_from_dict, _trade_to_dict
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


def candle(o, h, l, c, day=0):
    return OHLCVCandle(open=o, high=h, low=l, close=c, volume=1000,
                      timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=day))


def signal(short=False):
    return TradeSignal(symbol='TEST', timeframe='1d', pattern='double_top' if short else 'double_bottom',
                       action='SELL' if short else 'BUY', confidence=1, price=100, qty=100,
                       stop_loss=130 if short else 70, take_profit=70 if short else 130,
                       stop_loss_pct_cap=.05, trailing_stop_pct=.05,
                       trailing_activation_pct=.05, trailing_ref_after_check=True,
                       trailing_stop_mode='lowest_close' if short else 'highest_close',
                       provenance='patternsv2')


@pytest.mark.parametrize('short', [False, True])
def test_trail_arms_after_close_survives_restart_and_beats_hard_stop(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0)
    bar = candle(100, 104, 88, 90, 1) if short else candle(100, 112, 96, 110, 1)
    assert _check_exit(bar, p, 1) == (None, '')
    assert p._trailing_activated
    p = _trade_from_dict(_trade_to_dict(p))
    bar = candle(90, 110, 89, 108, 2) if short else candle(110, 111, 90, 92, 2)
    price, reason = _check_exit(bar, p, 2)
    assert reason == 'trailing_stop'
    assert price == pytest.approx(94.5 if short else 104.5)


@pytest.mark.parametrize('short', [False, True])
def test_hard_stop_gaps_fill_at_open(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0)
    bar = candle(110, 115, 108, 112) if short else candle(90, 92, 85, 88)
    assert _check_exit(bar, p, 1) == (bar.open, 'stop_loss')


@pytest.mark.parametrize('short', [False, True])
def test_target_touch_without_close_through(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0)
    bar = candle(99, 101, 69, 90) if short else candle(101, 131, 99, 110)
    assert _check_exit(bar, p, 1) == (70 if short else 130, 'take_profit')


def test_both_touched_stop_first_but_open_target_fills_first():
    p = _open_trade(signal(), candle(100, 100, 100, 100), 0)
    assert _check_exit(candle(100, 135, 90, 110), p, 1) == (95, 'stop_loss')
    p = _open_trade(signal(), candle(100, 100, 100, 100), 0)
    assert _check_exit(candle(135, 140, 90, 110), p, 1) == (135, 'take_profit')


def test_paper_uses_same_trail_and_accounting():
    account = PaperAccount(market='us', slippage_pct=0, txn_cost_pct=0)
    account.assume_session_open = True
    ok, message = account.open_position(signal(), candle(100, 100, 100, 100), OHLCVStore())
    assert ok, message
    assert account.on_bar('TEST', candle(100, 112, 96, 110, 1), '1d') == []
    closed = account.on_bar('TEST', candle(110, 111, 90, 92, 2), '1d')
    assert len(closed) == 1
    assert closed[0].exit_reason == 'trailing_stop'
    assert closed[0].exit_price == pytest.approx(104.5)
    assert account.cash == pytest.approx(account.initial_capital + closed[0].pnl_usd)


@pytest.mark.parametrize('short', [False, True])
def test_trail_never_loosens_and_gap_uses_open(short):
    p = _open_trade(signal(short), candle(100, 100, 100, 100), 0)
    first = candle(100, 101, 89, 90) if short else candle(100, 111, 99, 110)
    assert _check_exit(first, p, 1) == (None, '')
    retrace = candle(91, 93, 90, 92) if short else candle(109, 110, 107, 108)
    assert _check_exit(retrace, p, 2) == (None, '')
    assert (p.lowest_close_since_entry if short else p.highest_close_since_entry) == (90 if short else 110)
    gap = candle(97, 98, 95, 96) if short else candle(102, 103, 100, 101)
    assert _check_exit(gap, p, 3) == (gap.open, 'trailing_stop')


def test_structural_stop_tighter_than_cap_is_preserved():
    s = signal()
    s.stop_loss = 98
    p = _open_trade(s, candle(100, 100, 100, 100), 0)
    assert _check_exit(candle(100, 101, 97, 99), p, 1) == (98, 'stop_loss')


@pytest.mark.parametrize('short', [False, True])
def test_new_signals_protect_profit_after_activation_without_same_bar_lookahead(short):
    import pandas as pd
    from patterns_v2.signals import _to_signal

    df = pd.DataFrame({'close': [100.]}, index=pd.to_datetime(['2026-01-05']))
    raw = {
        'pattern': 'double_top' if short else 'double_bottom',
        'direction': -1 if short else 1, 'signal': 0, 'level': 100,
        'points': [(0, 130 if short else 70, 'high 1' if short else 'low 1')],
    }
    s = _to_signal(df, raw, symbol='TEST', timeframe='1d', latest=0)
    s.qty = 100
    account = PaperAccount(market='us', slippage_pct=0, txn_cost_pct=.001)
    account.assume_session_open = True
    ok, message = account.open_position(s, candle(100, 100, 100, 100), OHLCVStore())
    assert ok, message
    # Today's wide range cannot execute the stop established by today's close.
    activation = candle(100, 104, 94, 95, 1) if short else candle(100, 106, 96, 106, 1)
    assert account.on_bar('TEST', activation, '1d') == []
    p = account.positions['TEST'][0]
    p = _trade_from_dict(_trade_to_dict(p))
    account.positions['TEST'] = [p]
    assert p.resolved_rules['trailing_stop_pct'] == .025
    retrace = candle(95, 99, 94, 98, 2) if short else candle(106, 107, 101, 102, 2)
    closed = account.on_bar('TEST', retrace, '1d')
    assert len(closed) == 1
    assert closed[0].exit_reason == 'trailing_stop'
    assert closed[0].exit_price == pytest.approx(97.375 if short else 103.35)
    assert closed[0].pnl_pct > 2
    assert account.cash == pytest.approx(account.initial_capital + closed[0].pnl_usd)
