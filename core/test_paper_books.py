"""Dual-book paper manager: isolation, stream exclusive, ticker collision."""

from __future__ import annotations

import asyncio
import threading
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtester import BacktestTrade
from core.market import PH
from core.paper_books import PaperBook, PaperBookManager
from core import signal_log_store as sls


START_KW = dict(
    extra_symbols="",
    use_stream=False,
    kronos_gate=False,
    kronos_rank=False,
    kronos_batch=False,
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
        mgr.books["us"].account.positions["SM"] = _open_trade("SM", 10.0)
        mgr.books["ph"].account.positions["SM"] = _open_trade("SM", 100.0)
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


def test_kronos_infer_lock_does_not_overlap():
    from core.kronos_gate import kronos_infer_lock

    in_critical: list[int] = []
    overlap: list[bool] = []

    def worker() -> None:
        with kronos_infer_lock():
            if in_critical:
                overlap.append(True)
            in_critical.append(1)
            time.sleep(0.05)
            in_critical.pop()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert overlap == []


def test_ensure_stream_server_passes_history_url() -> None:
    from config import settings
    from core.paper_books import PaperBook

    prev = settings.stocks_history_url
    book = PaperBook("us")
    try:
        settings.stocks_history_url = "https://33ai.edos.uk"
        with patch("core.paper_books.subprocess.Popen") as popen, \
             patch("core.paper_books.time.sleep"), \
             patch.object(book, "_kill_whatever_is_on"), \
             patch.object(book, "_port_open", return_value=True):
            err = book._ensure_stream_server("2025-01-02")
        assert err is None
        env = popen.call_args.kwargs["env"]
        assert env["STOCKS_HISTORY_URL"] == "https://33ai.edos.uk"
        cmd = popen.call_args.args[0]
        assert "--papertrade-stream" in cmd
        assert "2025-01-02" in cmd
    finally:
        settings.stocks_history_url = prev
        if book._stream_proc is not None:
            book._stream_proc = None


def test_lamps_payload_is_running_flags_only() -> None:
    with patch("core.paper_books.PaperAccount.save"):
        mgr = PaperBookManager()
        payload = mgr.lamps()
    assert set(payload["books"]) == {"us", "ph"}
    assert payload["books"]["us"] == {"running": False}
    assert payload["books"]["ph"] == {"running": False}
    assert "clocks" not in payload
