from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from data.stream_server import StreamServer, _SymbolTape, _load_symbol_db


def test_stream_advance_moves_all_loaded_tapes_atomically(tmp_path: Path):
    server = StreamServer(base_dir=str(tmp_path))
    rows_a = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
        {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "timestamp": 2},
    ]
    rows_b = [
        {"open": 10, "high": 10, "low": 10, "close": 10, "volume": 1, "timestamp": 1},
        {"open": 20, "high": 20, "low": 20, "close": 20, "volume": 1, "timestamp": 2},
    ]
    server._tapes = {"A": _SymbolTape(rows_a), "B": _SymbolTape(rows_b)}
    assert server._tapes["A"].snapshot()["candle"]["timestamp"] == 1
    assert server._tapes["B"].snapshot()["candle"]["timestamp"] == 1
    server.advance()
    assert server._tapes["A"].snapshot()["candle"]["timestamp"] == 2
    assert server._tapes["B"].snapshot()["candle"]["timestamp"] == 2


def test_pinned_asof_skips_future_ipo_tape():
    """An IPO whose first bar is later than the control asof must not leak
    a future price into the current scan."""
    server = StreamServer(base_dir=".")
    liquid = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
        {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "timestamp": 2},
        {"open": 3, "high": 3, "low": 3, "close": 3, "volume": 1, "timestamp": 3},
    ]
    ipo = [
        {"open": 90, "high": 90, "low": 90, "close": 90, "volume": 1, "timestamp": 3},
    ]
    server._tapes = {"AAPL": _SymbolTape(liquid), "IPO": _SymbolTape(ipo)}
    assert server.pin_asof("AAPL") == 1
    assert server._tapes["AAPL"].snapshot(server._asof_ts)["candle"]["timestamp"] == 1
    assert server._tapes["IPO"].snapshot(server._asof_ts) is None
    server.advance()
    assert server._asof_ts == 2
    assert server._tapes["IPO"].snapshot(server._asof_ts) is None
    server.advance()
    assert server._asof_ts == 3
    assert server._tapes["IPO"].snapshot(server._asof_ts)["candle"]["close"] == 90


def test_load_symbol_db_uses_history_api_not_local_postgres():
    from config import settings

    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.history_client.history_api_configured", return_value=True), \
         patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("AAPL")
    assert out == rows
    load.assert_called_once_with(
        "AAPL", after_ts=None, limit=settings.papertrade_stream_lookback_bars,
    )
    get_conn.assert_not_called()


def test_load_symbol_db_skips_postgres_when_api_empty():
    with patch("data.history_client.history_api_configured", return_value=True), \
         patch("data.history.load_daily_tape_rows", return_value=None), \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("ZZZZ")
    assert out is None
    get_conn.assert_not_called()


def test_load_symbol_db_uses_after_ts_when_start_set():
    from config import settings

    start_ts = 1_700_000_000
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": start_ts}]
    with patch("data.history_client.history_api_configured", return_value=True), \
         patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("AAPL", start_ts=start_ts)
    assert out == rows
    lookback = settings.papertrade_stream_lookback_bars
    load.assert_called_once_with(
        "AAPL", after_ts=start_ts - lookback * 86400 * 2, limit=None,
    )
    get_conn.assert_not_called()
