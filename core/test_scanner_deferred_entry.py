"""Smallest possible check that a live/paper signal fills one bar late —
same one-bar deferral core/backtester.py's pending_entry gives backtests —
instead of the same candle whose close produced the signal."""

import asyncio
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import PATTERN_SCAN_HISTORY_BARS
from core import signal_log_store as sls
from core.paper_trader import PaperAccount
from core.scanner import MarketScanner
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, TradeSignal


def _candle(close: float, ts: datetime) -> OHLCVCandle:
    return OHLCVCandle(open=close, high=close, low=close, close=close, volume=1.0, timestamp=ts)


class _FirstBarBuyPattern(BasePattern):
    """Fires a BUY once, on the very first bar it sees; None afterwards —
    isolates whether the fill lands on that bar or the next one."""

    name = "test_pattern"
    fired = False

    @property
    def timeframes(self):
        return ["1d"]

    def analyze(self, snapshot, store):
        if self.fired:
            return None
        self.fired = True
        return TradeSignal(
            symbol=snapshot.symbol, action="BUY", pattern=self.name,
            timeframe="1d", confidence=0.9, price=snapshot.candle.close, qty=10,
            stop_loss=snapshot.candle.close * 0.90,
            take_profit=snapshot.candle.close * 1.20,
        )


class _FakeFeed:
    """Two-bar fake TVClient: bar 1 close=100, bar 2 close=110."""

    def __init__(self):
        self.bar = 0
        tz = ZoneInfo("America/New_York")
        # After 16:00 ET so 1d bars count as closed sessions.
        self.candles = [
            _candle(100.0, datetime(2024, 1, 2, 16, 5, tzinfo=tz)),
            _candle(110.0, datetime(2024, 1, 3, 16, 5, tzinfo=tz)),
        ]

    @asynccontextmanager
    async def mcp_session(self):
        yield None

    async def fetch_snapshot(self, symbol, timeframe, store=None, mcp_session=None):
        candle = self.candles[self.bar]
        snapshot = MarketSnapshot(
            symbol=symbol, timeframe=timeframe, timestamp=candle.timestamp,
            candle=candle, indicators={}, summary={}, oscillators={}, moving_avgs={},
        )
        if store is not None:
            if self.bar == 0:
                tz = candle.timestamp.tzinfo
                warmup = [
                    _candle(
                        90.0,
                        datetime(2023, 11, 20, 16, 5, tzinfo=tz) + timedelta(days=i),
                    )
                    for i in range(PATTERN_SCAN_HISTORY_BARS)
                ]
                store.replace_all(symbol, timeframe, warmup + [candle])
            else:
                store.push(snapshot)
        return snapshot


def demo():
    prev = sls._log_dir
    sls._log_dir = Path(tempfile.mkdtemp())
    try:
        _demo_body()
    finally:
        sls._log_dir = prev


def _demo_body():
    feed = _FakeFeed()
    paper = PaperAccount(initial_capital=100_000.0, slippage_pct=0.0)
    scanner = MarketScanner(
        symbols=["TEST"], paper_account=paper, data_feed=feed,
        kronos_gate=False, volume_gate=False, kronos_rank=False,
    )
    scanner._patterns = [_FirstBarBuyPattern()]

    asyncio.run(scanner._scan_all())
    assert "TEST" not in paper.positions, (
        "signal on bar 1 must NOT fill on bar 1 (same-bar execution)"
    )
    assert scanner._last_bar_ts.get(("TEST", "1d")) == date(2024, 1, 2)

    feed.bar = 1
    asyncio.run(scanner._scan_all())
    assert "TEST" in paper.positions, "signal on bar 1 must fill on bar 2"
    assert paper.positions["TEST"].entry_price == 110.0, (
        f"expected fill at bar 2's close (110.0), got {paper.positions['TEST'].entry_price}"
    )

    # Same session date, later last-print time is not a new daily bar.
    feed.candles[1] = _candle(112.0, datetime(2024, 1, 3, 18, 0, tzinfo=ZoneInfo("America/New_York")))
    asyncio.run(scanner._scan_all())
    assert paper.positions["TEST"].entry_price == 110.0

    print("deferred entry fill: all checks passed")


def test_ph_reference_symbol_prefers_bdo():
    scanner = MarketScanner(symbols=["ICT", "BDO", "ALI"], market="ph")
    assert scanner._reference_symbol() == "BDO"
    assert scanner._pin_candidates()[0] == "BDO"


def test_us_reference_symbol_prefers_spy():
    scanner = MarketScanner(symbols=["NVDA", "AAPL", "SPY"], market="us")
    assert scanner._reference_symbol() == "SPY"


def test_scanner_seeds_cooldown_from_closed_paper():
    from datetime import timezone

    from core.backtester import BacktestTrade
    from core.engine_defaults import passes_cooldown
    from patterns.base_pattern import TradeSignal

    paper = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    paper.closed.append(
        BacktestTrade(
            symbol="FOXO", timeframe="1d",
            pattern="pattern_007_descending_channel", action="BUY",
            entry_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
            exit_date=datetime(2026, 8, 2, tzinfo=timezone.utc),
            entry_price=1.0, exit_price=0.94, pnl=-10.0, pnl_pct=-6.0,
            qty=10, exit_bar_idx=20,
        )
    )
    scanner = MarketScanner(
        symbols=["FOXO"], paper_account=paper, data_feed=object(),
        kronos_gate=False, volume_gate=False, kronos_rank=False, market="us",
    )
    signal = TradeSignal(
        symbol="FOXO", timeframe="1d", pattern="pattern_003_double_bottom",
        action="BUY", price=5.0, confidence=0.9, qty=1,
    )
    assert not passes_cooldown(signal, 25, scanner._cooldown_tracker)
    assert passes_cooldown(signal, 30, scanner._cooldown_tracker)


def test_scanner_drops_no_data_but_retries_history_unavailable():
    from data.stream_client import FetchSkip

    class _SkipFeed:
        def __init__(self, code: str):
            self.code = code

        @asynccontextmanager
        async def mcp_session(self):
            yield None

        async def fetch_snapshot(self, symbol, timeframe, store=None, mcp_session=None):
            raise FetchSkip(self.code, f"{self.code} for {symbol}")

    dead = MarketScanner(
        symbols=["EXPH"], data_feed=_SkipFeed("no_data"),
        kronos_gate=False, volume_gate=False, kronos_rank=False,
    )
    dead._patterns = [_FirstBarBuyPattern()]
    asyncio.run(dead._scan_all())
    assert "EXPH" in dead._dead_symbols

    busy = MarketScanner(
        symbols=["KNSL"], data_feed=_SkipFeed("history_unavailable"),
        kronos_gate=False, volume_gate=False, kronos_rank=False,
    )
    busy._patterns = [_FirstBarBuyPattern()]
    asyncio.run(busy._scan_all())
    assert "KNSL" not in busy._dead_symbols
    assert busy.stats["snapshot_errors"] >= 1


def test_scanner_batch_fetch_snapshots_marks_no_data_dead():
    from data.stream_client import FetchSkip

    class _BatchSkipFeed:
        snapshot_batch_size = 50

        @asynccontextmanager
        async def mcp_session(self):
            yield None

        async def fetch_snapshot(self, symbol, timeframe, store=None, mcp_session=None):
            raise AssertionError("batch path should not call fetch_snapshot")

        async def fetch_snapshots(self, symbols, timeframe, store=None, mcp_session=None):
            return {
                s: FetchSkip("no_data", f"no data for {s}") for s in symbols
            }

    scanner = MarketScanner(
        symbols=["EXPH", "GYGC"], data_feed=_BatchSkipFeed(),
        kronos_gate=False, volume_gate=False, kronos_rank=False,
    )
    scanner._patterns = [_FirstBarBuyPattern()]
    asyncio.run(scanner._scan_all())
    assert "EXPH" in scanner._dead_symbols
    assert "GYGC" in scanner._dead_symbols


def test_scan_interval_zero_is_kept():
    scanner = MarketScanner(
        symbols=["AAPL"], scan_interval_seconds=0,
        kronos_gate=False, volume_gate=False, kronos_rank=False, market="us",
    )
    assert scanner._scan_interval == 0
    scanner.stats["scan_duration_s"] = 12.0
    asyncio.run(scanner._sleep_until_next_scan())


if __name__ == "__main__":
    demo()
