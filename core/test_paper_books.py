"""Dual-book paper manager: isolation, stream exclusive, ticker collision."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from core.backtester import BacktestTrade
from core.market import PH
from core.paper_books import PaperBook, PaperBookManager
from core import signal_log_store as sls


START_KW = dict(
    extra_symbols="",
    use_stream=False,
    volume_gate=False,
    stream_start=None,
)


def _idle_run(self, *args, **kwargs) -> None:
    """Stand-in for PaperBook._run_thread: hang on a real loop so stop() works."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def forever():
        while True:
            await asyncio.sleep(0.05)

    with self.lock:
        self.loop = loop
        self.task = loop.create_task(forever())
        self.status = "Running"
    try:
        loop.run_until_complete(self.task)
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        if not loop.is_closed():
            loop.close()
        with self.lock:
            self.running = False
            self.loop = None
            self.task = None
            self.status = "Stopped."


def _wait_loop(book: PaperBook, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if book.running and book.loop is not None and book.task is not None:
            return
        time.sleep(0.01)
    raise AssertionError(f"{book.market} book did not start a loop")


def _wait_stopped(book: PaperBook, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not book.running:
            return
        time.sleep(0.01)
    raise AssertionError(f"{book.market} book did not stop")


@contextmanager
def _manager():
    with patch("core.paper_books.PaperAccount.save"):
        with patch.object(PaperBook, "_run_thread", _idle_run):
            mgr = PaperBookManager()
            try:
                yield mgr
            finally:
                mgr.stop_all()
                for book in mgr.books.values():
                    t = book._thread
                    if t is not None and t.is_alive():
                        t.join(timeout=2.0)


def _open_trade(symbol: str, entry: float) -> BacktestTrade:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return BacktestTrade(
        symbol=symbol,
        timeframe="1d",
        pattern="pattern_003_double_bottom",
        action="BUY",
        entry_date=now,
        exit_date=now,
        entry_price=entry,
        exit_price=entry,
        pnl=0.0,
        pnl_pct=0.0,
        qty=10,
    )


def test_start_us_while_ph_running():
    with _manager() as mgr:
        assert mgr.start("ph", 10, **START_KW) is None
        _wait_loop(mgr.books["ph"])
        assert mgr.start("us", 10, **START_KW) is None
        _wait_loop(mgr.books["us"])
        assert mgr.books["us"].running and mgr.books["ph"].running
        assert mgr.books["us"]._thread is not mgr.books["ph"]._thread
        assert mgr.books["us"]._thread.name == "paper-us"
        assert mgr.books["ph"]._thread.name == "paper-ph"
        mgr.stop("us")
        _wait_stopped(mgr.books["us"])
        assert mgr.books["ph"].running


def test_start_twice_returns_already_running():
    with _manager() as mgr:
        assert mgr.start("us", 10, **START_KW) is None
        _wait_loop(mgr.books["us"])
        err = mgr.start("us", 10, **START_KW)
        assert err is not None
        assert "already running" in err.lower()


def test_start_laya_filter_refused_when_service_not_ready():
    with _manager() as mgr:
        with patch("core.laya_paper.laya_readiness",
                   return_value=(False, "LAYA_ENABLED is false")):
            err = mgr.start("us", 10, **{**START_KW, "laya_mode": "filter",
                                         "laya_policy_id": "laya-candidate-v1-default"})
        assert err is not None
        assert "not ready" in err.lower()
        assert not mgr.books["us"].running


def test_start_laya_shadow_refused_when_service_not_ready():
    with _manager() as mgr:
        with patch("core.laya_paper.laya_readiness",
                   return_value=(False, "missing-model")):
            err = mgr.start("ph", 10, **{**START_KW, "laya_mode": "shadow"})
        assert err is not None
        assert "not ready" in err.lower()
        assert not mgr.books["ph"].running


def test_start_laya_shadow_freezes_mode_and_policy():
    with _manager() as mgr:
        with patch("core.laya_paper.laya_readiness",
                   return_value=(True, "ready (fake)")):
            assert mgr.start("us", 10, **{**START_KW, "laya_mode": "shadow",
                                          "laya_policy_id": ""}) is None
        _wait_loop(mgr.books["us"])
        assert mgr.books["us"].laya_mode == "shadow"
        snap = mgr.snapshot("us")
        assert snap["laya"]["mode"] == "shadow"
        assert snap["laya"]["readiness"] == "shadow-ready"


def test_start_laya_rejects_unknown_mode():
    with _manager() as mgr:
        err = mgr.start("us", 10, **{**START_KW, "laya_mode": "autotrade"})
        assert err is not None
        assert "unsupported laya mode" in err.lower()
        assert not mgr.books["us"].running


def test_start_laya_filter_requires_policy():
    with _manager() as mgr:
        err = mgr.start("us", 10, **{**START_KW, "laya_mode": "filter",
                                     "laya_policy_id": ""})
        assert err is not None
        assert "policy" in err.lower()
        assert not mgr.books["us"].running


def test_start_duration_days_freezes_and_rejects_bad_value():
    with _manager() as mgr:
        assert mgr.start("us", 10, **{**START_KW, "duration_days": 30}) is None
        _wait_loop(mgr.books["us"])
        assert mgr.books["us"].duration_days == 30
        snap = mgr.snapshot("us")
        assert snap["duration_days"] == 30
        mgr.stop("us")
        _wait_stopped(mgr.books["us"])
    with _manager() as mgr:
        err = mgr.start("ph", 10, **{**START_KW, "duration_days": 0})
        assert err is not None
        assert "duration" in err.lower()
        assert not mgr.books["ph"].running


def test_start_duration_defaults_to_unlimited():
    with _manager() as mgr:
        assert mgr.start("us", 10, **START_KW) is None
        _wait_loop(mgr.books["us"])
        assert mgr.books["us"].duration_days is None


def test_reset_ph_does_not_wipe_us():
    with patch("core.paper_books.PaperAccount.save"):
        with patch("core.paper_books.reset_signal_log"):
            mgr = PaperBookManager()
            mgr.books["us"].account.cash = 4242.0
            err = mgr.reset("ph")
            assert err is None
            assert mgr.books["us"].account.cash == 4242.0
            assert mgr.books["ph"].account.cash == PH.paper_initial_capital
            assert mgr.books["ph"].account.positions == {}


def test_snapshot_reads_and_resets_signal_log_file():
    prev = sls._log_dir
    sls._log_dir = Path(tempfile.mkdtemp())
    try:
        with patch("core.paper_books.PaperAccount.save"):
            sls.append_signal_log("us", {"symbol": "TSLA", "status": "rejected", "reason": "gate"})
            mgr = PaperBookManager()
            snap = mgr.snapshot("us")
            assert any(r["symbol"] == "TSLA" for r in snap["signal_logs"])
            mgr.reset_logs("us")
            assert mgr.snapshot("us")["signal_logs"] == []
    finally:
        sls._log_dir = prev


def test_ticker_collision_chart_uses_market():
    with patch("core.paper_books.PaperAccount.save"):
        mgr = PaperBookManager()
        mgr.books["us"].account.positions["SM"] = [_open_trade("SM", 10.0)]
        mgr.books["ph"].account.positions["SM"] = [_open_trade("SM", 100.0)]
        snap = mgr.snapshot_all()
        us_sm = next(p for p in snap["books"]["us"]["positions"] if p["symbol"] == "SM")
        ph_sm = next(p for p in snap["books"]["ph"]["positions"] if p["symbol"] == "SM")
        assert us_sm["entry"] == 10.0
        assert ph_sm["entry"] == 100.0
        assert us_sm["market"] == "us" and ph_sm["market"] == "ph"

        df = pd.DataFrame(
            {
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [1.0, 1.0],
            },
            index=pd.date_range("2026-01-01", periods=2, tz="UTC"),
        )

        def _payload(_df, **kw):
            return {"symbol": kw["symbol"], "entry": kw["entry"]}

        with patch("data.history.load_daily_ohlcv_df", return_value=df):
            with patch(
                "analysis.chart_renderer.build_trade_viewer_payload",
                side_effect=_payload,
            ):
                us_chart = mgr.chart("us", side="open", symbol="SM")
                ph_chart = mgr.chart("ph", side="open", symbol="SM")
        assert us_chart["entry"] == 10.0
        assert ph_chart["entry"] == 100.0


def test_selected_log_chart_preserves_its_own_detection():
    rows = [
        {"symbol": "AAPL", "ts": "2024-01-02T00:00:00", "sim_bar": "2024-01-02", "price": 10,
         "pattern": "old_pattern", "action": "BUY", "chart_annotations": [{"type": "hline", "price": 9, "label": "neckline"}]},
        {"symbol": "AAPL", "ts": "2024-02-02T00:00:00", "price": 20, "pattern": "new_pattern"},
    ]
    with patch("core.paper_books.PaperAccount.save"), patch("core.paper_books.load_signal_log", return_value=rows):
        mgr = PaperBookManager()
        mgr.books["us"].account.positions["AAPL"] = [_open_trade("AAPL", 30)]
        with patch("data.history.load_daily_ohlcv_df", return_value=pd.DataFrame({"close": [10, 11]})), patch(
            "analysis.chart_renderer.build_trade_viewer_payload", return_value={"ok": True},
        ) as builder:
            result = mgr.chart("us", side="log", symbol="AAPL", log_time=rows[0]["ts"])
            assert result == {"ok": True, "trade_id": f"log:{rows[0]['ts']}:AAPL"}
            assert builder.call_args.kwargs["market"] == "us"
            assert builder.call_args.kwargs["entry"] == 10
            assert builder.call_args.kwargs["pattern"] == "old_pattern"
            assert builder.call_args.kwargs["entry_time"] == "2024-01-02"
            assert builder.call_args.kwargs["annotations"] == rows[0]["chart_annotations"]
        assert "error" in mgr.chart("us", side="log", symbol="AAPL", log_time="missing")


def test_log_chart_uses_open_then_closed_then_symbol():
    df = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 1.0],
        },
        index=pd.date_range("2026-01-01", periods=2, tz="UTC"),
    )

    def _payload(_df, **kw):
        return {
            "symbol": kw["symbol"],
            "entry": kw["entry"],
            "exit_price": kw.get("exit_price"),
            "pattern": kw.get("pattern"),
        }

    with patch("core.paper_books.PaperAccount.save"):
        mgr = PaperBookManager()
        with patch("data.history.load_daily_ohlcv_df", return_value=df):
            with patch(
                "analysis.chart_renderer.build_trade_viewer_payload",
                side_effect=_payload,
            ):
                mgr.books["us"].account.positions["AAPL"] = [_open_trade("AAPL", 10.0)]
                open_chart = mgr.chart("us", side="log", symbol="AAPL")
                assert open_chart["entry"] == 10.0
                assert open_chart["exit_price"] is None

                del mgr.books["us"].account.positions["AAPL"]
                closed = _open_trade("AAPL", 12.0)
                closed.exit_price = 13.0
                mgr.books["us"].account.closed.append(closed)
                closed_chart = mgr.chart("us", side="log", symbol="AAPL")
                assert closed_chart["entry"] == 12.0
                assert closed_chart["exit_price"] == 13.0

                mgr.books["us"].account.closed.clear()
                prev = sls._log_dir
                sls._log_dir = Path(tempfile.mkdtemp())
                try:
                    sls.append_signal_log("us", {
                        "symbol": "TSLA",
                        "status": "rejected",
                        "pattern": "pattern_003_double_bottom",
                        "action": "BUY",
                        "price": 250.0,
                        "timeframe": "1d",
                    })
                    log_chart = mgr.chart("us", side="log", symbol="TSLA")
                    assert log_chart["entry"] == 250.0
                    assert log_chart["pattern"] == "pattern_003_double_bottom"
                finally:
                    sls._log_dir = prev


def test_stream_exclusive():
    with _manager() as mgr:
        mgr.books["us"].running = True
        mgr.books["us"].use_stream = True
        err = mgr.start("ph", 10, **{**START_KW, "use_stream": True})
        assert err is not None
        assert "already in use by US" in err
        live = mgr.start("ph", 10, **START_KW)
        assert live is None
        _wait_loop(mgr.books["ph"])
        assert mgr.books["ph"].running
        assert mgr.books["us"].running


def test_edgar_skip_is_thread_local():
    from data.edgar_client import set_skip_edgar, skip_edgar_enabled

    barrier = threading.Barrier(2)
    us_seen: list[bool] = []
    ph_seen: list[bool] = []

    def worker(skip: bool, out: list[bool]) -> None:
        set_skip_edgar(skip)
        barrier.wait()
        out.append(skip_edgar_enabled())

    t_us = threading.Thread(target=worker, args=(False, us_seen))
    t_ph = threading.Thread(target=worker, args=(True, ph_seen))
    t_us.start()
    t_ph.start()
    t_us.join(timeout=2)
    t_ph.join(timeout=2)
    assert us_seen == [False]
    assert ph_seen == [True]


def test_port_open_works_inside_running_event_loop() -> None:
    """CLI `--paper --stream` already owns a loop; probe must not asyncio.run."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()[:2]

    def accept_one() -> None:
        """Answer the handshake the probe sends, then return to listening."""
        conn, _ = srv.accept()
        with conn:
            conn.recv(1024)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
            )

    answer = threading.Thread(target=accept_one, daemon=True)
    answer.start()
    try:
        async def _check() -> bool:
            return PaperBook._port_open(host, port)

        assert asyncio.run(_check()) is True
    finally:
        answer.join(timeout=5)
        srv.close()

    async def _closed() -> bool:
        return PaperBook._port_open(host, port)

    assert asyncio.run(_closed()) is False


def test_local_chart_ignores_scanner_tape() -> None:
    """Local double-click must read 33ai history, not the scanner's memory."""
    from core.paper_books import PaperBook

    api = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 1.0],
        },
        index=pd.date_range("2026-01-01", periods=2, tz="UTC"),
    )
    scanner = Mock()
    scanner.ohlcv_frame.return_value = api * 1000
    with patch("core.paper_books.PaperAccount.save"):
        book = PaperBook("us")
        book.scanner = scanner
        book.account.positions["AAPL"] = [_open_trade("AAPL", 10.0)]
        with patch("data.history.owns_local_stocks_history", return_value=False), \
             patch("data.history.load_daily_ohlcv_df", return_value=api) as load, \
             patch("analysis.chart_renderer.build_trade_viewer_payload", return_value={"ok": True}):
            assert book.render_trade_chart(side="open", symbol="AAPL") == {
                "ok": True, "trade_id": book.account.positions["AAPL"][0].trade_id}
    scanner.ohlcv_frame.assert_not_called()
    load.assert_called_once()
    assert load.call_args.args[0] == "AAPL"
    assert load.call_args.kwargs["market"] == "us"
    assert load.call_args.kwargs["tv_fallback"] is False


def test_paper_weekly_chart_aggregates_only_daily_history_fallback() -> None:
    from core.paper_books import _chart_history_frame

    idx = (pd.bdate_range("2024-03-04", periods=10, tz="America/New_York")
           + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC")
    daily = pd.DataFrame({
        "open": range(10, 20), "high": range(12, 22), "low": range(9, 19),
        "close": range(11, 21), "volume": 100,
    }, index=idx)
    scanner = Mock()
    scanner.ohlcv_frame.return_value = None
    for owns_history in (False, True):
        scanner.reset_mock()
        with patch("data.history.owns_local_stocks_history", return_value=owns_history), \
             patch("data.history.load_daily_ohlcv_df", return_value=daily) as load:
            weekly = _chart_history_frame(scanner, "TEST", "1W", market="us")
        assert list(weekly.index.strftime("%Y-%m-%d")) == ["2024-03-08", "2024-03-15"]
        assert list(weekly["close"]) == [15, 20]
        assert list(weekly["volume"]) == [500, 500]
        load.assert_called_once_with("TEST", tv_fallback=False, market="us")
        if owns_history:
            scanner.ohlcv_frame.assert_called_once_with("TEST", "1W", min_bars=2)
        else:
            scanner.ohlcv_frame.assert_not_called()

    # A scanner frame already has the requested frequency and its saved anchor
    # dates. It must pass through without reaggregation or relabeling.
    scanner.ohlcv_frame.return_value = weekly
    with patch("data.history.owns_local_stocks_history", return_value=True), \
         patch("data.history.load_daily_ohlcv_df") as load:
        assert _chart_history_frame(scanner, "TEST", "1W", market="us") is weekly
    load.assert_not_called()


def test_stream_server_reusable_fresh_and_resume() -> None:
    from core.paper_books import stream_server_reusable

    fresh = {
        "market": "us", "start_date": "2026-08-31", "asof_day": "", "loaded": 500,
    }
    assert stream_server_reusable(
        fresh, market="us", start_date="2026-08-31", resume=False,
    )
    advanced = {**fresh, "asof_day": "2026-09-15"}
    assert not stream_server_reusable(
        advanced, market="us", start_date="2026-08-31", resume=False,
    )
    assert stream_server_reusable(
        advanced, market="us", start_date="2026-09-14", resume=True,
    )
    assert not stream_server_reusable(
        advanced, market="ph", start_date="2026-09-14", resume=True,
    )
    assert not stream_server_reusable(
        advanced, market="us", start_date="2026-09-20", resume=True,
    )


def test_ensure_stream_server_reuses_matching_process() -> None:
    from core.paper_books import PaperBook

    book = PaperBook("us")
    status = {
        "market": "us", "start_date": "2026-08-31", "asof_day": "", "loaded": 1000,
    }
    with patch.object(book, "_port_open", return_value=True), \
         patch.object(book, "_probe_stream_status", return_value=status), \
         patch.object(book, "_kill_whatever_is_on") as kill, \
         patch("core.paper_books.subprocess.Popen") as popen:
        err = book._ensure_stream_server("2026-08-31", resume=False)
    assert err is None
    kill.assert_not_called()
    popen.assert_not_called()


def test_ensure_stream_server_passes_history_url() -> None:
    from config import settings
    from core.paper_books import PaperBook

    prev = settings.stocks_history_url
    book = PaperBook("us")
    try:
        settings.stocks_history_url = "https://33ai.rootlabs.dev"
        with patch("core.paper_books.subprocess.Popen") as popen, \
             patch("core.paper_books.time.sleep"), \
             patch.object(book, "_kill_whatever_is_on"), \
             patch.object(book, "_probe_stream_status", return_value=None), \
             patch.object(book, "_port_open", return_value=True):
            err = book._ensure_stream_server("2025-01-02")
        assert err is None
        env = popen.call_args.kwargs["env"]
        assert env["STOCKS_HISTORY_URL"] == "https://33ai.rootlabs.dev"
        cmd = popen.call_args.args[0]
        assert "--papertrade-stream" in cmd
        assert "--market" in cmd
        assert cmd[cmd.index("--market") + 1] == "us"
        assert "2025-01-02" in cmd
        assert env["MARKET"] == "us"
    finally:
        settings.stocks_history_url = prev
        if book._stream_proc is not None:
            book._stream_proc = None


def test_ensure_stream_server_passes_ph_market() -> None:
    book = PaperBook("ph")
    with patch("core.paper_books.subprocess.Popen") as popen, \
         patch("core.paper_books.time.sleep"), \
         patch.object(book, "_kill_whatever_is_on"), \
         patch.object(book, "_probe_stream_status", return_value=None), \
         patch.object(book, "_port_open", return_value=True):
        err = book._ensure_stream_server("2026-01-01")
    assert err is None
    cmd = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert cmd[cmd.index("--market") + 1] == "ph"
    assert env["MARKET"] == "ph"
    if book._stream_proc is not None:
        book._stream_proc = None


def test_ph_stream_snapshot_not_session_idle() -> None:
    book = PaperBook("ph")
    book.running = True
    book.use_stream = True
    with patch("core.paper_books.session_window", return_value="closed"):
        snap = book.snapshot()
    assert snap["session_idle"] is False
    assert "idle until AM/PM" not in snap["status"]


def test_ph_live_snapshot_session_idle_when_closed() -> None:
    book = PaperBook("ph")
    book.running = True
    book.use_stream = False
    with patch("core.paper_books.session_window", return_value="closed"):
        snap = book.snapshot()
    assert snap["session_idle"] is True
    assert "idle until AM/PM" in snap["status"]

    with patch("core.paper_books.PaperAccount.save"):
        mgr = PaperBookManager()
        payload = mgr.lamps()
    assert set(payload["books"]) == {"us", "ph"}
    assert payload["books"]["us"] == {"running": False}
    assert payload["books"]["ph"] == {"running": False}
    assert "clocks" not in payload


# ── stream-server readiness probe ────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_in_thread(port: int):
    """Run a real WebSocket server until the returned stop function is called."""
    import logging

    import websockets

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("websockets")
    logger.setLevel(logging.DEBUG)
    handler = _Capture()
    logger.addHandler(handler)
    ready, stop = threading.Event(), threading.Event()

    async def handler_fn(ws):
        await ws.wait_closed()

    def serve() -> None:
        async def run() -> None:
            async with websockets.serve(handler_fn, "127.0.0.1", port):
                ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.02)

        asyncio.run(run())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(10), "test WebSocket server did not start"

    def shutdown() -> None:
        stop.set()
        thread.join(timeout=10)
        logger.removeHandler(handler)

    return records, shutdown


def test_stream_probe_does_not_log_a_failed_handshake() -> None:
    """A bare connect made the server report 'opening handshake failed'."""
    port = _free_port()
    records, shutdown = _serve_in_thread(port)
    try:
        assert PaperBook._port_open("127.0.0.1", port) is True
    finally:
        shutdown()
    errors = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert errors == [], errors


def test_stream_probe_reports_a_closed_port_as_down() -> None:
    assert PaperBook._port_open("127.0.0.1", _free_port()) is False


def test_empty_book_replays_from_an_earlier_start() -> None:
    from core.paper_trader import PaperAccount, resolve_stream_start

    acct = PaperAccount(market="us")
    acct._sim_now = datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc)
    acct.mark_bar_processed("NIO", "1d", "datetime.date(2026, 10, 7)")
    start, rewind = resolve_stream_start(acct, "2026-09-08", "us")
    assert (start, rewind) == ("2026-09-08", True)
    acct.clear_scan_clock()
    assert acct.sim_now() is None
    assert acct.processed_bar_identities_snapshot() == {}


def test_book_with_trades_stays_on_the_saved_session() -> None:
    from core.paper_trader import PaperAccount, resolve_stream_start

    acct = PaperAccount(market="us")
    acct._sim_now = datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc)
    acct.closed.append(object())
    start, rewind = resolve_stream_start(acct, "2026-09-08", "us")
    assert (start, rewind) == ("2026-10-07", False)


def test_status_payload_drops_infinite_profit_factor() -> None:
    import json

    from core.paper_books import json_safe

    payload = {"metrics": {"profit_factor": float("inf"), "win_rate_pct": float("nan")}}
    clean = json_safe(payload)
    json.dumps(clean, allow_nan=False)
    assert clean["metrics"]["profit_factor"] is None
    assert clean["metrics"]["win_rate_pct"] is None
    assert payload["metrics"]["profit_factor"] == float("inf")


def test_same_day_start_does_not_rewind() -> None:
    from core.paper_trader import PaperAccount, resolve_stream_start

    acct = PaperAccount(market="us")
    acct._sim_now = datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc)
    start, rewind = resolve_stream_start(acct, "2026-10-07", "us")
    assert (start, rewind) == ("2026-10-07", False)
