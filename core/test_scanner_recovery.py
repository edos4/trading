"""A failed feed request must not lose a replay session or orphan workers."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from config import settings
from core.scanner import MarketScanner
from data.stream_client import FetchSkip


def _scanner(feed, monkeypatch, symbols=("AAA", "BBB")):
    scanner = MarketScanner(symbols=list(symbols), data_feed=feed, duration_days=1)
    scanner._patterns = [SimpleNamespace(timeframes=["1d"])]
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    return scanner


@pytest.mark.parametrize("failure", [None, FetchSkip("history_unavailable", "retry")])
def test_incomplete_scan_retries_date_before_advance_and_duration_stop(monkeypatch, failure):
    class Feed:
        snapshot_batch_size = 10
        failed = True
        advances = 0

        async def fetch_snapshots(self, symbols, timeframe, **kwargs):
            return {symbol: (failure if self.failed and symbol == "BBB" else symbol)
                    for symbol in symbols}

        async def advance_replay(self, session):
            self.advances += 1
            return True, False

    feed = Feed()
    scanner = _scanner(feed, monkeypatch)
    scanner._sim_days = 1
    scanner._running = True
    seen = []
    monkeypatch.setattr(scanner, "_apply_bar", lambda symbol, *_: (seen.append(symbol), None))
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert seen == ["AAA"]
    assert feed.advances == 0
    assert scanner.stats["scan_complete"] is False
    assert scanner._stop_if_duration_reached() is False
    feed.failed = False
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert seen == ["AAA", "AAA", "BBB"]
    assert feed.advances == 1
    assert scanner.stats["scan_complete"] is True
    assert scanner._stop_if_duration_reached() is True


def test_scan_cycle_stamps_the_scan_clock(monkeypatch):
    """A finished cycle must leave a start/finish stamp and a running total."""

    class Feed:
        snapshot_batch_size = 10

        async def fetch_snapshots(self, symbols, timeframe, **kwargs):
            return {symbol: None for symbol in symbols}

        async def advance_replay(self, session):
            return True, False

    scanner = _scanner(Feed(), monkeypatch, symbols=("AAA",))
    monkeypatch.setattr(scanner, "_apply_bar", lambda symbol, *_: (False, None))
    scanner._running = True
    assert scanner.scan_timing()["scans_completed"] == 0

    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    timing = scanner.scan_timing()
    assert timing["scans_completed"] == 1
    assert timing["scan_in_progress"] is False
    assert timing["scan_elapsed_s"] is None
    assert timing["scan_total_s"] == pytest.approx(timing["last_scan_duration_s"])
    assert (
        datetime.fromisoformat(timing["scan_finished_at"])
        >= datetime.fromisoformat(timing["scan_started_at"])
    )

    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    timing = scanner.scan_timing()
    assert timing["scans_completed"] == 2
    assert timing["avg_scan_s"] == pytest.approx(scanner.stats["scan_total_s"] / 2)


def test_run_stamps_the_run_clock(monkeypatch):
    """The paper tab's run clock must start when the loop starts and freeze."""
    from contextlib import asynccontextmanager

    scanner = MarketScanner.__new__(MarketScanner)
    scanner.stats = {}
    scanner._running = True
    scanner._scan_interval = 0
    scanner._duration_days = None
    scanner._sim_days = 0
    scanner._symbols = []
    scanner._tv = SimpleNamespace()
    scanner._paper = None

    async def noop(*args, **kwargs):
        return None

    async def one_cycle(*args, **kwargs):
        scanner._running = False

    @asynccontextmanager
    async def sessions(n):
        yield []

    monkeypatch.setattr(scanner, "_start_when_ready", noop)
    monkeypatch.setattr(scanner, "_preload_feed", noop)
    monkeypatch.setattr(scanner, "_scan_all", one_cycle)
    monkeypatch.setattr(scanner, "_open_feed_sessions", sessions)
    monkeypatch.setattr(scanner, "stop", lambda: None)

    asyncio.run(scanner.run())
    assert scanner.stats["run_started_at"]
    assert scanner.stats["run_finished_at"]
    assert scanner.stats["run_finished_at"] >= scanner.stats["run_started_at"]
    assert scanner.scan_timing()["run_elapsed_s"] >= 0


def test_transport_failure_finishes_other_workers_before_reconnecting(monkeypatch):
    monkeypatch.setattr(settings, "scanner_concurrency", 2)
    started = asyncio.Event()
    completed = []

    class Feed:
        advances = 0

        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            if symbol == "AAA":
                await started.wait()
                raise ConnectionError("broken feed")
            started.set()
            await asyncio.sleep(0)
            completed.append(symbol)
            return None

        async def advance_replay(self, session):
            self.advances += 1
            return True, False

    feed = Feed()
    scanner = _scanner(feed, monkeypatch)
    with pytest.raises(ConnectionError, match="broken feed"):
        asyncio.run(scanner._scan_all(feed_sessions=[object(), object()]))
    assert completed == ["BBB"]
    assert feed.advances == 0


@pytest.mark.parametrize("batched", [False, True])
def test_failed_timeframe_preserves_signals_from_prior_timeframe(monkeypatch, batched):
    class Feed:
        requests = 0

        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            self.requests += 1
            if self.requests == 2:
                raise ConnectionError("second timeframe failed")
            return SimpleNamespace(symbol=symbol, timeframe=timeframe)

    feed = Feed()
    if batched:
        async def fetch_snapshots(symbols, timeframe, **kwargs):
            return {symbol: await feed.fetch_snapshot(symbol, timeframe) for symbol in symbols}

        feed.fetch_snapshots = fetch_snapshots
        feed.snapshot_batch_size = 10
    scanner = _scanner(feed, monkeypatch, symbols=("AAA",))
    scanner._patterns = [SimpleNamespace(timeframes=["1d", "1W"])]
    scanner._collect_first = True
    analyzed, flushed = [], []

    async def analyze(snapshots):
        analyzed.extend(snapshots)

    async def flush():
        flushed.extend(analyzed)

    monkeypatch.setattr(scanner, "_apply_bar", lambda symbol, timeframe, snapshot: (True, snapshot))
    monkeypatch.setattr(scanner, "_run_analyzes", analyze)
    monkeypatch.setattr(scanner, "_flush_collect_first", flush)
    with pytest.raises(ConnectionError, match="second timeframe failed"):
        asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert len(analyzed) == len(flushed) == 1
    assert analyzed[0].symbol == "AAA"


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), 0.0])
def test_invalid_bar_retries_before_recording_processed_watermark(monkeypatch, bad_price):
    from datetime import datetime, timezone

    from core.paper_trader import PaperAccount
    from data.tv_client import MarketSnapshot, OHLCVCandle

    stamp = datetime(2024, 1, 2, 21, 5, tzinfo=timezone.utc)

    class Feed:
        advances = 0
        price = bad_price

        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            return MarketSnapshot(
                symbol, timeframe, stamp,
                OHLCVCandle(100, 101, 99, self.price, 1000, stamp), {}, {}, {}, {},
            )

        async def advance_replay(self, session):
            self.advances += 1
            return True, False

    feed = Feed()
    account = PaperAccount(market="us")
    scanner = MarketScanner(symbols=["AAA"], data_feed=feed, paper_account=account, market="us")
    scanner._patterns = [SimpleNamespace(timeframes=["1d"])]
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    monkeypatch.setattr(scanner, "_has_ready_pattern", lambda *args: True)
    analyzed = []

    async def analyze(snapshots):
        analyzed.extend(snapshots)

    monkeypatch.setattr(scanner, "_run_analyzes", analyze)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert not scanner._persisted_bar_ids
    assert not scanner._last_bar_ts
    assert not analyzed and feed.advances == 0
    feed.price = 100
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert "AAA|1d" in scanner._persisted_bar_ids
    assert len(analyzed) == feed.advances == 1


def test_repeated_invalid_bar_does_not_stall_replay(monkeypatch):
    from datetime import datetime, timezone

    from data.tv_client import MarketSnapshot, OHLCVCandle

    stamp = datetime(2024, 1, 2, 21, 5, tzinfo=timezone.utc)

    class Feed:
        advances = 0

        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            return MarketSnapshot(
                symbol, timeframe, stamp,
                OHLCVCandle(100, 101, 99, float("nan"), 1000, stamp), {}, {}, {}, {},
            )

        async def advance_replay(self, session):
            self.advances += 1
            return True, False

    feed = Feed()
    scanner = MarketScanner(symbols=["GTVH"], data_feed=feed, market="ph")
    scanner._patterns = [SimpleNamespace(timeframes=["1d"])]
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    analyzed = []

    async def analyze(snapshots):
        analyzed.extend(snapshots)

    monkeypatch.setattr(scanner, "_run_analyzes", analyze)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert feed.advances == 0
    assert scanner.stats["scan_complete"] is False
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert feed.advances == 1
    assert scanner.stats["scan_complete"] is True
    assert not analyzed
    assert not scanner._persisted_bar_ids


def test_incomplete_scan_paced_replay_waits(monkeypatch):
    scanner = MarketScanner(
        symbols=["AAPL"], scan_interval_seconds=0, market="us",
    )
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("core.scanner.asyncio.sleep", fake_sleep)
    scanner.stats["scan_complete"] = False
    asyncio.run(scanner._sleep_until_next_scan())
    scanner.stats["scan_complete"] = True
    asyncio.run(scanner._sleep_until_next_scan())
    assert slept == [1.0]


def test_completed_scans_accumulate_on_the_book_clock(monkeypatch):
    """The clock is the book's total, so a fresh run continues it."""

    class Feed:
        snapshot_batch_size = 10

        async def fetch_snapshots(self, symbols, timeframe, **kwargs):
            return {symbol: None for symbol in symbols}

        async def advance_replay(self, session):
            return True, False

    from core.paper_trader import PaperAccount

    acct = PaperAccount(market="us")
    scanner = MarketScanner(symbols=["AAA"], data_feed=Feed(), paper_account=acct)
    scanner._patterns = [SimpleNamespace(timeframes=["1d"])]
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    monkeypatch.setattr(scanner, "_apply_bar", lambda symbol, *_: (False, None))
    scanner._running = True

    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    timing = scanner.scan_timing()
    assert timing["scans_completed"] == 2
    assert timing["scan_total_s"] == pytest.approx(
        acct.scan_clock()["scan_total_s"])

    # A new run builds a new scanner on the same account: the total stays.
    restarted = MarketScanner(
        symbols=["AAA"], data_feed=Feed(), paper_account=acct,
    )
    monkeypatch.setattr(restarted, "_note_worker", lambda: None)
    assert restarted.scan_timing()["scans_completed"] == 2
