"""Malformed execution observations must not mutate a paper book."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from copy import deepcopy

import pytest

from core.candle_validation import candle_error
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle
from data.stream_client import StreamClient, FetchSkip
from data.ohlcv_store import OHLCVStore


@pytest.mark.parametrize('changes', [
    {'close': 80}, {'high': 98}, {'low': 102}, {'volume': -1},
    {'volume': float('nan')}, {'timestamp': None},
    {'timestamp': datetime(2026, 1, 1)}, {'close': float('inf')},
])
def test_bad_bar_cannot_settle_or_mark_a_held_trade(changes):
    book, p = book_with_position(False)
    before = (book.cash, book.equity(), deepcopy(p.paper_protection), deepcopy(p.position_marks))
    bad = replace(candle(100, 101, 99, 100, 1), **changes)
    assert candle_error(bad)
    assert book.on_bar('TEST', bad, '1d') == []
    assert before == (book.cash, book.equity(), p.paper_protection, p.position_marks)
    assert book.open_count() == 1 and book.bar_count('TEST', '1d') == 0


def test_stream_validates_before_overwriting_store_and_warm_flag():
    client, store = StreamClient(), OHLCVStore()
    original = candle(100, 101, 99, 100)
    store.replace_all('TEST', '1d', [original])
    bad = dict(open=100, high=101, low=99, close=80, volume=1,
               timestamp=int(candle(0,0,0,0,1).timestamp.timestamp()))
    with pytest.raises(FetchSkip, match='OHLC range'):
        client._snapshot_from_reply('TEST','1d',{'candle':bad,'history':[bad]},store)
    assert store.copy_candles('TEST','1d') == [original]
    assert not client._warm


def test_flat_zero_volume_observation_is_valid():
    assert candle_error(replace(candle(100,100,100,100), volume=0)) is None


def test_rejected_entry_candle_cannot_reset_the_daily_loss_budget():
    from core.test_patternsv2_exits import signal
    book,p=book_with_position(False)
    book._daily_key='2026-01-05'; book._daily_pnl=-1500
    bad=candle(100,101,99,80,1)
    ok,reason=book.open_position(replace(signal(),symbol='OTHER'),bad,OHLCVStore())
    assert not ok and 'Invalid trade candle' in reason
    assert book._daily_key=='2026-01-05' and book._daily_pnl==-1500
