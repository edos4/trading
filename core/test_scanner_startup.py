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


def test_duration_stop_fires_on_session_count():
    scanner = _scanner()
    scanner._duration_days = 3
    scanner._sim_days = 0
    scanner._running = True
    scanner.stats = {"scan_complete": True}
    assert scanner._stop_if_duration_reached() is False
    assert scanner._running is True
    scanner._sim_days = 3
    assert scanner._stop_if_duration_reached() is True
    assert scanner._running is False


def test_duration_unlimited_never_stops():
    scanner = _scanner()
    scanner._duration_days = None
    scanner._sim_days = 10_000
    scanner._running = True
    scanner.stats = {"scan_complete": True}
    assert scanner._stop_if_duration_reached() is False
    assert scanner._running is True


def test_duration_waits_for_complete_scan():
    scanner = _scanner()
    scanner._duration_days = 3
    scanner._sim_days = 5
    scanner._running = True
    scanner.stats = {"scan_complete": False}
    assert scanner._stop_if_duration_reached() is False
    assert scanner._running is True


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


def test_one_failed_signal_does_not_drop_the_rest():
    from datetime import datetime, timezone

    from data.tv_client import MarketSnapshot, OHLCVCandle
    from patterns.base_pattern import TradeSignal

    scanner = _scanner()
    scanner.stats = {"patterns_found": 0, "signals_rejected": 0}
    seen = []
    logged = []

    async def process(signal, _pattern, _candle):
        seen.append(signal.symbol)
        if signal.symbol == "FGBI":
            raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    scanner._process_signal = process
    scanner._record_detection = lambda _signal: None
    scanner._append_signal_log = (
        lambda signal, *, status, reason: logged.append((signal.symbol, status))
    )
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    snap = MarketSnapshot(
        symbol="FGBI", timeframe="1d", timestamp=now,
        candle=OHLCVCandle(8.42, 8.43, 8.33, 8.42, 8000, now),
        indicators={}, summary={}, oscillators={}, moving_avgs={},
    )

    def _signal(symbol, pattern):
        return TradeSignal(
            symbol=symbol, action="BUY", pattern=pattern, timeframe="1d",
            confidence=0.9, price=8.42, qty=0.0, provenance="patternsv2",
        )

    asyncio.run(scanner._emit_signals(snap, [
        _signal("FGBI", "falling_wedge"),
        _signal("RLAY", "double_top"),
    ]))
    assert seen == ["FGBI", "RLAY"]
    assert logged == [("FGBI", "rejected")]
    assert scanner.stats["patterns_found"] == 2
    assert scanner.stats["signals_rejected"] == 1


def test_stop_tolerates_database_outage(monkeypatch):
    import core.pattern_loader as loader

    def unavailable(worker, stopped=False):
        raise DatabaseUnavailable("PostgreSQL unavailable or operation timed out")

    monkeypatch.setattr(loader, "acknowledge_worker", unavailable)
    _scanner().stop()
