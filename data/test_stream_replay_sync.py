from __future__ import annotations

from datetime import date
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
    from data.stream_tape_cache import CACHE_MISS

    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.stream_server.cache_get", return_value=CACHE_MISS), \
         patch("data.stream_server.cache_put"), \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("AAPL")
    assert out == rows
    load.assert_called_once_with(
        "AAPL", after_ts=None, limit=settings.papertrade_stream_lookback_bars,
        market=None,
    )
    get_conn.assert_not_called()


def test_load_symbol_db_skips_postgres_when_api_empty():
    from data.stream_tape_cache import CACHE_MISS

    with patch("data.history.load_daily_tape_rows", return_value=None), \
         patch("data.stream_server.cache_get", return_value=CACHE_MISS), \
         patch("data.stream_server.cache_put") as put, \
         patch("data.db.get_conn") as get_conn:
        out = _load_symbol_db("ZZZZ")
    put.assert_not_called()
    assert out is None
    get_conn.assert_not_called()


def test_load_symbol_db_uses_after_ts_when_start_set():
    from config import settings

    start_ts = 1_700_000_000
    from data.stream_tape_cache import CACHE_MISS

    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": start_ts}]
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.stream_server.cache_get", return_value=CACHE_MISS), \
         patch("data.stream_server.cache_put"), \
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

    from data.stream_tape_cache import CACHE_MISS

    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.history.load_daily_tape_rows", return_value=rows) as load, \
         patch("data.stream_server.cache_get", return_value=CACHE_MISS), \
         patch("data.stream_server.cache_put"):
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


def test_pinned_advance_returns_zero_at_end_of_tape():
    server = StreamServer()
    rows = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
    ]
    server._tapes = {"AAPL": _SymbolTape(rows)}
    assert server.pin_asof("AAPL") == 1
    assert server.advance() == 0
    assert server._asof_ts == 1


def test_client_advance_replay_reports_end_of_tape():
    import asyncio
    import json

    from data.stream_client import StreamClient

    class _FakeSession:
        async def send(self, _msg):
            pass

        async def recv(self):
            return json.dumps({"advanced": 0, "asof_day": "2026-08-31", "end": True})

    ok, reached_end = asyncio.run(StreamClient().advance_replay(_FakeSession()))
    assert ok is True
    assert reached_end is True


def test_client_advance_replay_not_end_while_more_bars():
    import asyncio
    import json

    from data.stream_client import StreamClient

    class _FakeSession:
        async def send(self, _msg):
            pass

        async def recv(self):
            return json.dumps({"advanced": 3, "asof_day": "2026-08-30"})

    ok, reached_end = asyncio.run(StreamClient().advance_replay(_FakeSession()))
    assert ok is True
    assert reached_end is False



def test_delta_snapshot_omits_history():
    tape = _SymbolTape([
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
        {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "timestamp": 2},
    ])
    full = tape.snapshot()
    assert "history" in full
    assert full["history"][-1] == full["candle"]
    delta = tape.snapshot(include_history=False)
    assert "history" not in delta
    assert delta["candle"] == full["candle"]


def test_batch_snapshots_history_for_subset():
    server = StreamServer()
    server._tapes = {
        "A": _SymbolTape([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
        ]),
        "B": _SymbolTape([
            {"open": 9, "high": 9, "low": 9, "close": 9, "volume": 1, "timestamp": 1},
        ]),
    }
    out = server.snapshots_payload(["A", "B"], history_for={"A"})
    assert "history" in out["results"]["A"]
    assert "history" not in out["results"]["B"]
    assert out["results"]["B"]["candle"]["close"] == 9


def test_preload_symbols_fetches_missing_tapes():
    server = StreamServer()
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    with patch("data.stream_server._load_symbol_db", return_value=rows) as load, \
         patch("data.history_bulk.load_daily_tape_rows_bulk", return_value=None):
        summary = server.preload_symbols(["aaa", "AAA", "bbb"])
    assert summary["loaded"] == 2
    assert summary["symbols"] == 2
    assert load.call_count == 2
    assert "AAA" in server._tapes
    assert "BBB" in server._tapes


def test_store_apply_candle_appends_then_replaces_same_ts():
    from datetime import datetime, timezone

    from data.ohlcv_store import OHLCVStore
    from data.tv_client import OHLCVCandle

    store = OHLCVStore(window=8)
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 6, tzinfo=timezone.utc)
    first = OHLCVCandle(1, 1, 1, 1, 1, t0)
    nxt = OHLCVCandle(2, 2, 2, 2, 1, t1)
    same = OHLCVCandle(3, 3, 3, 3, 1, t1)
    store.apply_candle("AAPL", "1d", first)
    store.apply_candle("AAPL", "1d", nxt)
    assert store.available("AAPL", "1d") == 2
    store.apply_candle("AAPL", "1d", same)
    assert store.available("AAPL", "1d") == 2
    assert store.latest_close("AAPL", "1d") == 3.0
    copied = store.copy_candles("AAPL", "1d")
    assert len(copied) == 2
    assert copied[-1].close == 3.0


def test_client_delta_hydrates_store_without_rewriting_history():
    from datetime import datetime, timezone

    from data.ohlcv_store import OHLCVStore
    from data.stream_client import StreamClient
    from data.tv_client import OHLCVCandle

    store = OHLCVStore(window=8)
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    client = StreamClient()
    seed = OHLCVCandle(1, 1, 1, 1, 1, t0)
    store.replace_all("AAPL", "1d", [seed])
    ts = int(t0.timestamp())
    snap = client._snapshot_from_reply(
        "AAPL",
        "1d",
        {"candle": {
            "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1,
            "timestamp": ts + 86400,
        }},
        store,
    )
    assert snap.candle.close == 2
    assert store.available("AAPL", "1d") == 2
    assert ("AAPL", "1d") in client._warm
    assert client._needs_history("AAPL", "1d", store) is False


def test_preload_uses_bulk_then_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAM_TAPE_CACHE_DIR", str(tmp_path))
    rows = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1}]
    server = StreamServer(market="us")
    with patch(
        "data.history_bulk.load_daily_tape_rows_bulk",
        return_value={"AAA": rows, "BBB": []},
    ) as bulk, patch("data.history.load_daily_tape_rows") as single:
        summary = server.preload_symbols(["AAA", "BBB"])
    assert summary["loaded"] == 1
    assert summary["empty"] == 1
    bulk.assert_called_once()
    single.assert_not_called()

    again = StreamServer(market="us")
    with patch("data.history_bulk.load_daily_tape_rows_bulk") as bulk2, \
         patch("data.history.load_daily_tape_rows") as single2:
        summary2 = again.preload_symbols(["AAA", "BBB"])
    assert summary2["loaded"] == 1
    assert summary2["empty"] == 1
    bulk2.assert_not_called()
    single2.assert_not_called()


def test_status_payload_reports_market_start_and_asof():
    server = StreamServer(start_date="2026-08-31", market="us")
    payload = server.status_payload()
    assert payload["market"] == "us"
    assert payload["start_date"] == "2026-08-31"
    assert payload["asof_day"] == ""
    assert payload["loaded"] == 0
    server._asof_ts = server._start_ts
    assert server.status_payload()["asof_day"] == "2026-08-31"


def test_indexed_tape_matches_last_same_day_print_and_skips_missing_days():
    from datetime import datetime, timezone

    def ts(day, hour):
        return int(datetime(2026, 8, day, hour, tzinfo=timezone.utc).timestamp())

    tape = _SymbolTape([
        {"timestamp": ts(3, 13), "close": 1},
        {"timestamp": ts(3, 21), "close": 2},
        {"timestamp": ts(5, 21), "close": 3},
    ])
    assert tape.index_for_asof(ts(2, 21)) is None
    assert tape.index_for_asof(ts(3, 0)) == 1
    assert tape.snapshot(ts(3, 0), include_history=False)["candle"]["close"] == 2
    assert tape.index_for_asof(ts(4, 21)) is None
    assert tape.index_for_asof(ts(6, 21)) is None
    assert tape.next_ts_after(ts(2, 21)) == ts(3, 13)
    assert tape.next_ts_after(ts(3, 0)) == ts(5, 21)
    assert tape.next_ts_after(ts(4, 21)) == ts(5, 21)
    assert tape.next_ts_after(ts(5, 0)) is None
    empty = _SymbolTape([])
    assert empty.index_for_asof(ts(3, 0)) is None
    assert empty.next_ts_after(ts(3, 0)) is None
    assert empty.snapshot() is None


def test_degenerate_duplicate_print_cannot_shadow_the_session_bar():
    """A zero-price re-ingest row must not stand in for a real session bar."""
    from datetime import datetime, timezone

    def ts(day, hour):
        return int(datetime(2026, 8, day, hour, tzinfo=timezone.utc).timestamp())

    real = {"open": 0.002, "high": 0.002, "low": 0.002, "close": 0.002,
            "volume": 0, "timestamp": ts(17, 13)}
    junk = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
            "volume": 90000, "timestamp": ts(17, 14)}
    tape = _SymbolTape([real, junk])
    assert tape.rows == [real]
    assert tape.index_for_asof(ts(17, 21)) == 0
    assert tape.snapshot(ts(17, 21), include_history=False)["candle"]["close"] == 0.002

    server = StreamServer()
    server._tapes = {"AAPT": tape}
    server._asof_ts = ts(17, 21)
    assert server._snapshot_message("AAPT")["candle"]["close"] == 0.002

    # A session whose only print is degenerate is not a bar at all: the replay
    # skips it (asof_mismatch) instead of handing the scanner a bad candle on
    # every scan, which would keep the scan incomplete and pin the date.
    degraded = _SymbolTape([junk])
    assert degraded.rows == [] and degraded.snapshot(ts(17, 21)) is None
    server._tapes = {"AAPT": degraded}
    skip = server._snapshot_message("AAPT")
    assert skip["code"] == "asof_mismatch"


def test_stream_client_transport_failure_requests_reconnection():
    import asyncio
    import pytest
    from data.stream_client import StreamClient

    class BrokenSession:
        async def send(self, _message):
            raise OSError("disconnected")

    with pytest.raises(ConnectionError, match="request failed"):
        asyncio.run(StreamClient().fetch_snapshots(["AAA"], mcp_session=BrokenSession()))


def test_missing_or_malformed_symbol_reply_is_retryable_and_keeps_other_symbols():
    import asyncio
    import json
    from data.stream_client import FetchSkip, StreamClient

    class Session:
        async def send(self, _message):
            pass

        async def recv(self):
            return json.dumps({"results": {
                "BBB": {"candle": {"timestamp": "invalid"}},
                "CCC": {"candle": {"open": 1, "high": 1, "low": 1,
                                   "close": 1, "volume": 1, "timestamp": 1}},
            }})

    result = asyncio.run(StreamClient().fetch_snapshots(
        ["AAA", "BBB", "CCC"], mcp_session=Session(),
    ))
    for symbol in ("AAA", "BBB"):
        assert isinstance(result[symbol], FetchSkip)
        assert result[symbol].code == "history_unavailable"
    assert result["CCC"].candle.close == 1


def _us_ts(day: int, month: int = 1, hour: int = 21) -> int:
    from datetime import datetime, timezone

    return int(datetime(2026, month, day, hour, tzinfo=timezone.utc).timestamp())


def test_advance_skips_a_us_holiday_print_only_foreign_tapes_carry():
    """MLK Day 2026-01-19: foreign/OTC listings print, cash-session names don't.

    Publishing that date made every cash-session symbol asof_mismatch and a held
    symbol stopped the whole book after its bounded retry.
    """
    server = StreamServer(market="us")
    try:
        cash = _SymbolTape([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "timestamp": _us_ts(15)},
            {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1,
             "timestamp": _us_ts(16)},
            {"open": 3, "high": 3, "low": 3, "close": 3, "volume": 1,
             "timestamp": _us_ts(20)},
        ])
        otc = _SymbolTape([
            {"open": 9, "high": 9, "low": 9, "close": 9, "volume": 1,
             "timestamp": _us_ts(16)},
            {"open": 8, "high": 8, "low": 8, "close": 8, "volume": 1,
             "timestamp": _us_ts(19)},
            {"open": 7, "high": 7, "low": 7, "close": 7, "volume": 1,
             "timestamp": _us_ts(20)},
        ])
        server._tapes = {"AAPL": cash, "OTC1": otc}
        server._asof_ts = _us_ts(16)

        assert server.advance() > 0
        assert server.status_payload()["asof_day"] == "2026-01-20"
        assert server._snapshot_message("AAPL")["candle"]["close"] == 3
        assert server._snapshot_message("OTC1")["candle"]["close"] == 7
        assert server.advance() == 0
        assert server.status_payload()["asof_day"] == "2026-01-20"
    finally:
        server._load_pool.shutdown(wait=True)


def test_resumed_replay_is_rolled_off_a_closed_session():
    """A book saved on a holiday cannot be marked there, so resume must not
    re-enter the date that stopped it."""
    server = StreamServer(market="us")
    try:
        cash = _SymbolTape([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
             "timestamp": _us_ts(16)},
            {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1,
             "timestamp": _us_ts(20)},
        ])
        server._tapes = {"AAPL": cash}
        server._asof_ts = _us_ts(19)

        pinned = server.pin_asof("AAPL")
        assert pinned == _us_ts(20)
        assert server.status_payload()["asof_day"] == "2026-01-20"
        assert server._snapshot_message("AAPL")["candle"]["close"] == 2
    finally:
        server._load_pool.shutdown(wait=True)


def test_session_start_rolls_forward_past_floating_us_holidays():
    from data.stream_server import _is_likely_cash_session, _parse_start_ts, asof_key

    def rolled(day: str) -> str | None:
        ts = _parse_start_ts(day, market="us")
        return None if ts is None else str(asof_key(ts))

    assert rolled("2026-01-19") == "2026-01-20"   # MLK Day (Monday)
    assert rolled("2026-07-03") == "2026-07-06"   # Sat 4 July observed Friday
    assert rolled("2026-11-26") == "2026-11-27"   # Thanksgiving
    # PH keeps its own calendar: MLK Day is a normal PSE session.
    assert _is_likely_cash_session(date(2026, 1, 19), "ph") is True
    assert _is_likely_cash_session(date(2026, 1, 19), "us") is False

