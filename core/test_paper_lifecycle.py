"""Session startup, cancellation and stream ownership stay consistent."""

import threading
from unittest.mock import Mock, patch

import pytest

from core.paper_books import PaperAccount, PaperBook, PaperBookManager


@pytest.mark.parametrize("failure", ["universe", "scanner"])
def test_startup_failure_releases_book(failure):
    with patch.object(PaperAccount, "load", side_effect=lambda **kw: PaperAccount(**kw)), \
         patch.object(PaperAccount, "save"), \
         patch("core.paper_books.TVClient.fetch_universe_cached", return_value=[("AAPL", "NASDAQ")]) as universe, \
         patch("core.paper_books.MarketScanner") as scanner:
        (universe if failure == "universe" else scanner).side_effect = RuntimeError("unavailable")
        book = PaperBook("us")
        assert book.start(10, use_stream=False, volume_gate=False) is None
        book._thread.join(timeout=3)
        assert not book._thread.is_alive()
        assert not book.running
        assert book.loop is None and book.task is None
        assert "failed" in book.error.lower()
        assert book.reset() is None


def test_stop_during_symbol_fetch_prevents_scanner_start():
    fetching, release = threading.Event(), threading.Event()

    def fetch(*args, **kwargs):
        fetching.set()
        assert release.wait(3)
        return [("AAPL", "NASDAQ")]

    with patch.object(PaperAccount, "load", side_effect=lambda **kw: PaperAccount(**kw)), \
         patch("core.paper_books.TVClient.fetch_universe_cached", side_effect=fetch), \
         patch("core.paper_books.MarketScanner") as scanner:
        book = PaperBook("us")
        try:
            assert book.start(10, use_stream=False, volume_gate=False) is None
            assert fetching.wait(3)
            book.stop()
        finally:
            release.set()
            book._thread.join(timeout=3)
        scanner.assert_not_called()
        assert not book.running
        assert book.status == "Stopped."
        assert book.error is None


def test_concurrent_stream_starts_reserve_one_market():
    starting, release = threading.Event(), threading.Event()
    with patch.object(PaperAccount, "load", side_effect=lambda **kw: PaperAccount(**kw)):
        manager = PaperBookManager()

    def start_us(*args, **kwargs):
        starting.set()
        assert release.wait(3)
        manager.books["us"].running = True
        manager.books["us"].use_stream = True

    ph_start = Mock()
    results = {}
    with patch.object(manager.books["us"], "start", side_effect=start_us), \
         patch.object(manager.books["ph"], "start", ph_start):
        def run(market):
            results[market] = manager.start(market, 10, use_stream=True, volume_gate=False)

        us = threading.Thread(target=run, args=("us",))
        ph = threading.Thread(target=run, args=("ph",))
        us.start()
        try:
            assert starting.wait(3)
            ph.start()
        finally:
            release.set()
            us.join(timeout=3)
            if ph.ident is not None:
                ph.join(timeout=3)
        assert results["us"] is None
        assert "already in use by US" in results["ph"]
        ph_start.assert_not_called()


def test_restart_waits_until_outer_thread_cleanup_finishes():
    finished, release = threading.Event(), threading.Event()
    original_run = PaperBook._run_thread

    async def run():
        return None

    def pause_before_outer_cleanup(book, *args):
        original_run(book, *args)
        finished.set()
        assert release.wait(3)

    with patch.object(PaperAccount, "load", side_effect=lambda **kw: PaperAccount(**kw)), \
         patch.object(PaperAccount, "save"), \
         patch("core.paper_books.TVClient.fetch_universe_cached", return_value=[("AAPL", "NASDAQ")]), \
         patch("core.paper_books.MarketScanner") as scanner, \
         patch.object(PaperBook, "_run_thread", pause_before_outer_cleanup):
        scanner.return_value.run = run
        book = PaperBook("us")
        assert book.start(10, use_stream=False, volume_gate=False) is None
        worker = book._thread
        try:
            assert finished.wait(3)
            assert book.running
            assert "already running" in book.start(10, use_stream=False, volume_gate=False)
            assert book._thread is worker
        finally:
            release.set()
            worker.join(timeout=3)
        assert not book.running
        assert book.loop is None and book.task is None
