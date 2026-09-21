"""Scanner startup must survive an editor-database outage, not end the book."""

import asyncio

import pytest

from core.pattern_editor_db import DatabaseUnavailable
from core.scanner import MarketScanner


def _scanner(interval: float = 60.0) -> MarketScanner:
    scanner = MarketScanner.__new__(MarketScanner)
    scanner._scan_interval = interval
    scanner._running = False
    scanner._patterns = []
    scanner._analyze_pool = None
    return scanner


def test_start_retries_until_versions_resolve(monkeypatch):
    scanner = _scanner()
    attempts = []

    def start():
        attempts.append(1)
        if len(attempts) < 3:
            raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    scanner.start = start
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(scanner._start_when_ready())

    assert len(attempts) == 3
    assert sleeps == [60.0, 60.0]


def test_retry_wait_is_cancellable(monkeypatch):
    scanner = _scanner()
    attempts = []

    def start():
        attempts.append(1)
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    scanner.start = start

    async def scenario():
        task = asyncio.create_task(scanner._start_when_ready())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert attempts


def test_stop_tolerates_database_outage(monkeypatch):
    import core.pattern_loader as loader

    def unavailable(worker, stopped=False):
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    monkeypatch.setattr(loader, "acknowledge_worker", unavailable)
    _scanner().stop()
