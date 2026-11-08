"""Entry universe changes and slow feeds must not orphan protective exits."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.backtester import _make_snapshot
from core.scanner import MarketScanner
from core.test_patternsv2_exits import candle, signal
from core.test_position_protection import book_with_position
from data.ohlcv_store import OHLCVStore


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("patterns_enabled", [False, True])
def test_removed_symbol_and_timeframe_keep_their_cost_aware_stop(
        monkeypatch, short, batched, patterns_enabled):
    book, position = book_with_position(short, slip=.002)
    requests, analyses = [], []

    class Feed:
        snapshot_batch_size = 10

        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            requests.append((symbol, timeframe))
            bar = candle(100, 115, 85, 100, 1) if symbol == "TEST" else candle(100, 101, 99, 100, 1)
            return _make_snapshot(symbol, timeframe, bar)

        async def advance_replay(self, session):
            return True, True

    feed = Feed()
    if batched:
        async def fetch_many(symbols, timeframe, **kwargs):
            return {symbol: await feed.fetch_snapshot(symbol, timeframe, **kwargs) for symbol in symbols}
        feed.fetch_snapshots = fetch_many
    scanner = MarketScanner(symbols=["OTHER"], data_feed=feed, paper_account=book, market="us")
    scanner._patterns = [SimpleNamespace(timeframes=["1W"])] if patterns_enabled else []
    scanner._dead_symbols.add("TEST")  # Previously marked dead still gets retried while held.
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    monkeypatch.setattr(scanner, "_has_ready_pattern", lambda *args: True)

    async def analyze(snapshots):
        analyses.extend((s.symbol, s.timeframe) for s in snapshots)

    monkeypatch.setattr(scanner, "_run_analyzes", analyze)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert ("TEST", "1d") in requests
    assert ("TEST", "1W") not in requests
    assert analyses == ([("OTHER", "1W")] if patterns_enabled else [])
    assert book.closed == [position]
    assert position.exit_reason == "stop_loss"
    assert position.pnl_pct == pytest.approx(-3)
    assert book.cash == pytest.approx(book.initial_capital + position.pnl_usd)
    assert not book.execution_watchlist()


@pytest.mark.parametrize("short", [False, True])
def test_queued_order_keeps_its_next_open_feed_after_selection_changes(monkeypatch, short):
    book, old = book_with_position(short)
    book.on_bar("TEST", candle(100, 115, 85, 100, 1), "1d")
    assert old in book.closed
    queued = replace(signal(short), symbol="QUEUED")
    assert book.queue_position(queued, candle(100, 100, 100, 100, 2), OHLCVStore())[0]

    class Feed:
        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            assert (symbol, timeframe) == ("QUEUED", "1d")
            return _make_snapshot(symbol, timeframe, candle(100, 101, 99, 100, 3))

        async def advance_replay(self, session):
            return True, True

    scanner = MarketScanner(symbols=["OTHER"], market="us", data_feed=Feed(), paper_account=book)
    scanner._patterns = []
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert not book.pending_entries
    assert book.open_count() == 1
    assert book.open_for("QUEUED")[0].action == queued.action


@pytest.mark.parametrize("short", [False, True])
def test_live_quote_closes_before_the_rest_of_the_funded_scan_finishes(monkeypatch, short):
    book, position = book_with_position(short, slip=.002)
    book.assume_session_open = False
    price = 104 if short else 96

    class Feed:
        async def fetch_snapshot(self, symbol, timeframe, **kwargs):
            if symbol == "OTHER":
                # Waiting until funded_snapshots are drained leaves this false.
                assert book.closed == [position]
            snap = _make_snapshot(symbol, timeframe, candle(price, price, price, price, 1))
            snap.timestamp = position.entry_date + timedelta(seconds=5)
            return snap

    scanner = MarketScanner(symbols=["OTHER"], market="us", data_feed=Feed(), paper_account=book)
    scanner._patterns = [SimpleNamespace(timeframes=["1d"])]
    monkeypatch.setattr(scanner, "_note_worker", lambda: None)
    monkeypatch.setattr(scanner, "_has_ready_pattern", lambda *args: False)
    monkeypatch.setattr("core.scanner.is_closed_session_bar", lambda *args, **kwargs: False)
    asyncio.run(scanner._scan_all(feed_sessions=[object()]))
    assert book.closed == [position]
    assert position.exit_price == pytest.approx(price * (1.002 if short else .998))
    assert book.cash == pytest.approx(book.initial_capital + position.pnl_usd)
    assert scanner._cooldown_tracker[(position.symbol, position.pattern)][1] is True
