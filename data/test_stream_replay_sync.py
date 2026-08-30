from __future__ import annotations

from unittest.mock import patch

from data.stream_server import StreamServer, _SymbolTape, _load_symbol_db
from data.stream_client import LOCAL_STREAM_WS


def test_local_stream_ws_disables_protocol_pings():
    assert LOCAL_STREAM_WS["ping_interval"] is None
    assert LOCAL_STREAM_WS["ping_timeout"] is None


def test_stream_advance_moves_all_loaded_tapes_atomically():
    server = StreamServer()
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
    server = StreamServer()
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
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("AAPL")
    assert out == rows
    load.assert_called_once_with(
        "AAPL", after_ts=None, limit=settings.papertrade_stream_lookback_bars,
        market=None,
    )
    get_conn.assert_not_called()


def test_load_symbol_db_skips_postgres_when_api_empty():
    with patch("data.history.load_daily_tape_rows", return_value=None), \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("ZZZZ")
    assert out is None
    get_conn.assert_not_called()


def test_load_symbol_db_uses_after_ts_when_start_set():
    from config import settings

    start_ts = 1_700_000_000
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": start_ts}]
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("AAPL", start_ts=start_ts)
    assert out == rows
    lookback = settings.papertrade_stream_lookback_bars
    _, kwargs = load.call_args
    assert kwargs["after_ts"] == start_ts - lookback * 86400 * 2
    assert kwargs["limit"] >= lookback
    assert kwargs["market"] is None
    get_conn.assert_not_called()


def test_load_symbol_db_passes_ph_market():
    from config import settings

    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load:
        out = _load_symbol_db("BDO", market="ph")
    assert out == rows
    load.assert_called_once_with(
        "BDO", after_ts=None, limit=settings.papertrade_stream_lookback_bars,
        market="ph",
    )


def test_stream_server_tape_load_uses_market():
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    server = StreamServer(market="ph")
    with patch("data.stream_server._load_symbol_db", return_value=rows) as load:
        tape = server._tape_for("BDO")
    assert tape is not None
    load.assert_called_once_with("BDO", start_ts=server._start_ts, market="ph")


def test_pin_asof_keeps_existing_control_date_when_symbol_missing():
    server = StreamServer()
    liquid = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
    ]
    server._tapes = {"BDO": _SymbolTape(liquid)}
    assert server.pin_asof("BDO") == 1
    assert server.pin_asof("ICT") == 1


def test_parse_start_ts_rolls_new_years_to_next_session():
    from data.stream_server import _parse_start_ts, asof_key

    ts = _parse_start_ts("2026-01-01", market="us")
    assert ts is not None
    assert asof_key(ts) == "2026-01-02"


def test_pin_asof_skips_new_years_print_on_control_tape():
    """Illiquid control names can carry a Jan 1 print. Pinning that date
    made the rest of the US universe asof_mismatch with zero signals."""
    from datetime import datetime, timezone

    server = StreamServer(start_date="2026-01-01", market="us")
    nyd = int(datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc).timestamp())
    jan2 = int(datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc).timestamp())
    jan5 = int(datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": nyd},
        {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "timestamp": jan2},
        {"open": 3, "high": 3, "low": 3, "close": 3, "volume": 1, "timestamp": jan5},
    ]
    server._tapes = {"BHE": _SymbolTape(rows, start_ts=server._start_ts)}
    # start_date roll already prefers Jan 2; even if cursor sat on NYD, pin
    # must not publish 2026-01-01 as the universe control date.
    server._tapes["BHE"].cursor = 0
    pinned = server.pin_asof("BHE")
    assert pinned == jan2
    assert server._asof_ts == jan2


def test_tape_lookup_timeout_is_history_unavailable_not_cached():
    server = StreamServer()
    with patch("data.stream_server._load_symbol_db", return_value=None):
        tape, err = server._tape_lookup("KNSL")
    assert tape is None
    assert err == "history_unavailable"
    assert "KNSL" not in server._tapes
    assert "KNSL" not in server._known_empty
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.stream_server._load_symbol_db", return_value=rows):
        tape, err = server._tape_lookup("KNSL")
    assert err is None
    assert tape is not None


def test_tape_lookup_empty_is_no_data_and_cached():
    server = StreamServer()
    with patch("data.stream_server._load_symbol_db", return_value=[]) as load:
        tape, err = server._tape_lookup("EXPH")
        tape2, err2 = server._tape_lookup("EXPH")
    assert tape is None and err == "no_data"
    assert tape2 is None and err2 == "no_data"
    load.assert_called_once()


def test_pin_asof_reports_history_unavailable():
    server = StreamServer()
    with patch("data.stream_server._load_symbol_db", return_value=None):
        ts, err = server._pin_asof("CBIO")
    assert ts is None
    assert err == "history_unavailable"

