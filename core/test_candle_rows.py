"""Session normalization must produce valid pattern-worker datasets."""
from datetime import datetime

from core.pattern_edit_validation import candle_rows, validate_candles
from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle


def test_session_updates_produce_unique_ordered_dataset():
    def candle(stamp, close):
        return OHLCVCandle(open=close, high=close, low=close, close=close,
                          volume=1, timestamp=datetime.fromisoformat(stamp))

    store = OHLCVStore()
    # Out-of-order history spanning DST, plus a reprint of the same session
    # with a different timestamp (so deduplication must follow normalization).
    bars = [candle('2026-03-09T13:30:00+00:00', 3),
            candle('2026-03-06T14:30:00+00:00', 1),
            candle('2026-03-06T21:00:00+00:00', 2)]
    store.replace_all('CYDY', '1d', bars)
    frame = store.get_df('CYDY', '1d', min_bars=2)
    rows = candle_rows(frame, 'America/New_York')
    assert [row['time'] for row in rows] == ['2026-03-06', '2026-03-09']
    assert [row['close'] for row in rows] == [2, 3]
    validate_candles(rows)
    assert len(frame) == 3  # serialization does not mutate the cached store frame
    assert store.copy_candles('CYDY', '1d') == bars

    store.apply_candle('CYDY', '1d', candle('2026-03-09T20:00:00+00:00', 4))
    updated = store.get_df('CYDY', '1d')
    rows = candle_rows(updated, 'America/New_York')
    assert [row['close'] for row in rows] == [2, 4]
    validate_candles(rows)
