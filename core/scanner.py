"""
core/scanner.py — The market scanner. Replaces both the trading engine
and the webhook server from the previous architecture.

Every SCAN_INTERVAL_SECONDS it:
  1. Fetches fresh data from TradingView MCP for every (symbol, timeframe) pair
  2. Pushes the new candle into OHLCVStore
  3. Runs each registered pattern's analyze() method
  4. If a signal is returned:
        a. Kronos 3d forecast gate (if enabled) — direction + 3% in 3 days
        b. Renders a chart (if vision is enabled)
        c. Asks Claude vision to confirm the pattern
        d. Risk gates (ATR trail / R:R) then Kronos/vision; queue pending
           for next closed bar fill (same deferral as the backtester)

Concurrency: symbols are processed in parallel across N feed sessions
(one session per worker, controlled by scanner_concurrency). Paper stream
uses batch snapshots (papertrade_stream_batch_size) so each worker's
round-trip covers many symbols; after the first fill only the new candle
is sent. pattern.analyze() runs in a spawn process pool
(scanner_analyze_workers) against a copied candle list — the paper ledger
and Kronos/vision gates stay on this loop. A tqdm progress bar shows
scan progress on the CLI.
"""

from __future__ import annotations
import asyncio
import importlib
import pkgutil
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, AsyncExitStack
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

import patterns as patterns_pkg
from patterns.base_pattern import BasePattern, skip_pattern_module, TradeSignal

from data.tv_client import TVClient, MarketSnapshot
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.stream_client import FetchSkip
from analysis.chart_renderer import ChartRenderer
from analysis.vision_checker import VisionChecker, VisionVerdict
from core.paper_trader import PaperAccount
from core.kronos_gate import kronos_gate_check, kronos_gate_check_many
from core.kronos_rank_sleeve import is_kronos_rank_signal, run_sleeve
from core.backtester import describe_risk_gate_rejection
from core.engine_defaults import (
    describe_confidence_rejection,
    describe_cooldown_rejection,
    describe_min_share_price_rejection,
    describe_regime_rejection,
    passes_min_confidence,
    regime_filter_required,
    risk_gate_kwargs,
    seed_cooldown_from_trades,
    signal_reward_risk,
    structure_filters_enabled,
)
from analysis.price_volume import volume_confirm_gate

# TODO: re-enable IBKR when TWS/Gateway is available
# from broker.ibkr_client import IBKRClient
# from broker.order_manager import OrderManager
# from risk.risk_guard import RiskGuard, TradeIntent
from config import PATTERN_SCAN_HISTORY_BARS, settings
from core.market import (
    bar_identity,
    get_market,
    is_closed_session_bar,
    is_swing_timeframe,
    is_weekly_timeframe,
)
from core.signal_log_store import (
    SIGNAL_LOG_MAX,
    append_signal_log,
    load_signal_log,
)
from utils.logger import log

PATTERNS_DETECTED_FILE = Path("patterns_detected.md")
EXCLUDED_PATTERNS: set[str] = set()


class MarketScanner:
    def __init__(
        self,
        symbols: list[str] | None = None,
        exchange_overrides: dict[str, str] | None = None,
        paper_account: PaperAccount | None = None,
        disabled_patterns: list[str] | None = None,
        data_feed=None,
        scan_interval_seconds: int | None = None,
        kronos_gate: bool | None = None,
        volume_gate: bool | None = None,
        kronos_rank: bool | None = None,
        kronos_batch: bool | None = None,
        market: str | None = None,
        pattern_only: bool = False,
        collect_first: bool | None = None,
        collect_first_top_n: int | None = None,
        duration_days: int | None = None,
    ):
        self._symbols = symbols or settings.symbols
        self._disabled_patterns = set(disabled_patterns or [])
        profile = get_market(market if market is not None else getattr(paper_account, "market", None))
        self._market = profile.id
        self._min_share_price = profile.min_share_price
        from data.edgar_client import set_skip_edgar

        set_skip_edgar(profile.skip_edgar)
        self._scan_interval = (
            profile.scan_interval_seconds
            if scan_interval_seconds is None
            else int(scan_interval_seconds)
        )
        # None → follow settings; explicit True/False lets UI/CLI override for a session.
        self._kronos_gate = (
            profile.kronos_gate_default if kronos_gate is None else kronos_gate
        )
        self._volume_gate = (
            settings.volume_gate_enabled if volume_gate is None else volume_gate
        )
        self._kronos_rank = (
            profile.kronos_rank_default if kronos_rank is None else kronos_rank
        )
        # Collect-then-batch Kronos only when this is on. Sequential check()
        # remains the default.
        self._kronos_batch = (
            settings.kronos_batch_enabled if kronos_batch is None else kronos_batch
        )
        self._pattern_only = bool(pattern_only)
        self._collect_first = (
            settings.collect_first_enabled if collect_first is None else collect_first
        )
        self._collect_first_top_n = (
            settings.collect_first_top_n
            if collect_first_top_n is None
            else max(1, int(collect_first_top_n))
        )
        self._duration_days = (
            None if duration_days is None else max(1, int(duration_days))
        )
        self._tv = data_feed or TVClient(
            profile.tv_screener,
            profile.tv_exchange,
            exchange_overrides=exchange_overrides,
        )
        self._store = OHLCVStore(
            window=max(DEFAULT_WINDOW, settings.tv_history_days),
            session_tz=profile.session_tz,
        )
        self._renderer = ChartRenderer(save_to_disk=False, session_tz=profile.session_tz)
        self._vision = VisionChecker()
        # self._client   = IBKRClient()
        # self._orders   = OrderManager(self._client)
        # self._risk     = RiskGuard(self._client)
        self._paper = paper_account
        if self._paper is not None:
            self._paper.pattern_only = self._pattern_only
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._analyze_pool = None
        self._analyze_workers = 1
        self._running = False
        # Last *session* bar identity per (symbol, timeframe) — daily/weekly
        # keys are session dates, not last-print timestamps, so hourly scans
        # of a forming 1d candle do not count as new bars.
        self._last_bar_ts: dict[tuple[str, str], object] = {}
        # Persisted bar identities prevent a clean restart from processing
        # the same completed daily/weekly bar twice. Keep the in-memory object
        # representation above for existing tests, and compare a stable string
        # key against the persisted ledger state.
        self._persisted_bar_ids: dict[str, str] = (
            paper_account.processed_bar_identities_snapshot()
            if paper_account is not None
            and hasattr(paper_account, "processed_bar_identities_snapshot")
            else {}
        )
        # Unique completed daily-session dates observed in this scanner run.
        # Counting per symbol/timeframe was wrong: 1,000 symbols would advance
        # the replay-day counter 1,000 times in a single market session.
        self._sim_day_keys: set[str] = set()
        self._sim_days: int = 0
        self._dead_symbols: set[str] = set()
        self._thin_logged: set[tuple[str, str]] = set()
        # Signal detected on bar i, filled on bar i+1's close — mirrors the
        # backtester's pending_entry deferral (core/backtester.py) so paper/
        # live trading isn't more optimistic than the backtest that validated
        # the strategy (filling on the very candle whose close triggered it).
        self._pending_entries: dict[tuple[str, str], TradeSignal] = {}
        # Same (symbol, pattern) → (exit_bar_count, was_loss) cooldown map the
        # backtester uses — without this, paper re-entered losers immediately
        # while backtests waited cooldown_bars. Seed from the persisted ledger
        # so a scanner restart does not forget a just-stopped name.
        self._cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {}
        if self._paper is not None:
            seed_cooldown_from_trades(self._cooldown_tracker, self._paper.closed)
        # Kronos rank sleeve: only re-forecast when the daily asof advances
        # (hourly scans otherwise waste GPU on the same bar).
        self._kronos_rank_last_asof: object | None = None
        self._pending_kronos: list[tuple[TradeSignal, BasePattern | None, object]] = []
        self._pending_kronos_lock = asyncio.Lock()
        # Collect-first: chart-pattern signals gathered during a scan, ranked
        # after the scan completes, then only the top-N finished.
        self._collect_pool: list[tuple[TradeSignal, BasePattern | None, object]] = []
        # Scan-cycle health counters — surfaced by the paper trading UI/CLI
        # so a stalled or misbehaving scan is visible without reading logs.
        self.stats: dict = {
            "last_scan_at": None,
            "scan_duration_s": 0.0,
            "patterns_found": 0,
            "signals_rejected": 0,
            "volume_gate_rejected": 0,
            "kronos_rank_emitted": 0,
            "collect_first_ranked": 0,
            "collect_first_selected": 0,
            "trades_opened": 0,
            "sim_days": 0,
            # Rejection counts are grouped by the first gate that vetoed the
            # signal. This makes a high aggregate "Rejected" count actionable
            # without requiring a user to grep bot.log.
            "rejection_by_gate": {},
            # Scan-health counters make a suspiciously fast "0 patterns"
            # cycle diagnosable without reading the raw log.
            "symbols_total": len(self._symbols),
            "symbols_with_snapshot": 0,
            "snapshot_errors": 0,
            "timeframe_requests": 0,
            "timeframe_snapshots_ok": 0,
            "new_bars": 0,
            "pattern_evaluations": 0,
            "daily_date_skew": 0,
            "daily_dates_seen": 0,
            "asof_skipped": 0,
            "dead_symbols": 0,
        }
        # Ring buffer of per-signal accept/reject decisions. Persisted to
        # logs/paper_signals_{market}.jsonl — the web Paper Logs tab reads
        # that file, not this deque. Thread-safe — scan workers run concurrently.
        self._signal_log: deque[dict] = deque(
            load_signal_log(self._market), maxlen=SIGNAL_LOG_MAX,
        )
        self._signal_log_lock = threading.Lock()

    def signal_log_snapshot(self) -> list[dict]:
        with self._signal_log_lock:
            return list(self._signal_log)

    def clear_signal_log_memory(self) -> None:
        with self._signal_log_lock:
            self._signal_log.clear()

    def _append_signal_log(
        self,
        signal: TradeSignal,
        *,
        status: str,
        reason: str,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market": self._market,
            "symbol": signal.symbol,
            "timeframe": signal.timeframe,
            "action": signal.action,
            "pattern": signal.pattern,
            "confidence": round(float(signal.confidence), 4),
            "price": round(float(signal.price), 4) if signal.price is not None else None,
            "status": status,
            "reason": reason,
            "sim_bar": (
                signal.signal_bar_timestamp.isoformat()
                if signal.signal_bar_timestamp is not None else None
            ),
            "sim_bar_idx": signal.signal_bar_idx,
        }
        with self._signal_log_lock:
            self._signal_log.append(entry)
        try:
            append_signal_log(self._market, entry)
        except OSError:
            log.exception(f"Signal log | failed to persist {self._market} row")

        if status == "rejected":
            reason_lower = reason.lower()
            if reason_lower.startswith("execution r:r rejection"):
                gate = "execution_rr"
            elif "portfolio full" in reason_lower:
                gate = "max_positions"
            elif "gross exposure cap" in reason_lower:
                gate = "gross_exposure"
            elif "insufficient cash" in reason_lower:
                gate = "cash"
            elif "lot rounding" in reason_lower:
                gate = "lot_rounding"
            elif "session " in reason_lower:
                gate = "session"
            elif "collect-first" in reason_lower:
                gate = "collect_first"
            elif "confidence" in reason_lower:
                gate = "confidence"
            elif "share-price" in reason_lower or "share price" in reason_lower:
                gate = "min_price"
            elif "regime" in reason_lower or "sma200" in reason_lower:
                gate = "regime"
            elif "cooldown" in reason_lower:
                gate = "cooldown"
            elif "long-only" in reason_lower:
                gate = "long_only"
            elif "kronos" in reason_lower:
                gate = "kronos"
            elif "volume" in reason_lower:
                gate = "volume"
            elif "vision" in reason_lower:
                gate = "vision"
            elif "risk gate" in reason_lower:
                gate = "risk"
            else:
                gate = "other"
            stats = getattr(self, "stats", None)
            if not isinstance(stats, dict):
                return
            by_gate = stats.setdefault("rejection_by_gate", {})
            by_gate[gate] = by_gate.get(gate, 0) + 1

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        # self._client.connect()
        self._discover_patterns()
        self._init_patterns_detected_file()
        for p in self._patterns:
            p.on_start()
        self._open_analyze_pool()
        self._running = True
        log.info(
            f"Scanner started | "
            f"symbols={self._symbols} | "
            f"patterns={[p.name for p in self._patterns]} | "
            f"kronos_gate={'ON' if self._kronos_gate else 'OFF'} | "
            f"kronos_rank={'ON' if self._kronos_rank else 'OFF'} | "
            f"kronos_batch={'ON' if self._kronos_batch else 'OFF'} | "
            f"volume_gate={'ON' if self._volume_gate else 'OFF'} | "
            f"collect_first={'ON' if self._collect_first else 'OFF'} | "
            f"duration_days="
            f"{self._duration_days if self._duration_days is not None else 'unlimited'} | "
            f"analyze_workers={self._analyze_workers} | "
            f"interval={self._scan_interval}s"
        )

    def stop(self) -> None:
        self._running = False
        self._close_analyze_pool()
        for p in self._patterns:
            p.on_stop()
        # self._client.disconnect()
        log.info("Scanner stopped")

    def _stop_if_duration_reached(self) -> bool:
        """End the scan loop after N unique market sessions (sim_days)."""
        if self._duration_days is None:
            return False
        if self._sim_days < self._duration_days:
            return False
        log.info(
            f"Scanner | duration-days={self._duration_days} reached "
            f"(sim_days={self._sim_days}) — stopping"
        )
        self._running = False
        return True

    def _open_analyze_pool(self) -> None:
        from core.pattern_jobs import analyze_worker_count, make_analyze_pool

        self._close_analyze_pool()
        n = analyze_worker_count()
        self._analyze_workers = n
        self._analyze_pool = make_analyze_pool(
            disabled=self._disabled_patterns,
            session_tz=get_market(self._market).session_tz,
            skip_edgar=get_market(self._market).skip_edgar,
            window=max(DEFAULT_WINDOW, settings.tv_history_days),
            workers=n,
        )
        if self._analyze_pool is not None:
            log.info(f"Scanner | pattern analyze pool: {n} spawn workers")

    def _close_analyze_pool(self) -> None:
        pool = self._analyze_pool
        self._analyze_pool = None
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)

    async def _sleep_until_next_scan(self) -> None:
        """Sleep only the unused remainder of the scan interval.

        Stream replay already advances once per completed scan. Interval 0
        means scan-paced (no leftover sleep). Sleeping a full 60s after a
        long scan just idles the replay.
        """
        if self._scan_interval <= 0:
            return
        elapsed = float(self.stats.get("scan_duration_s") or 0.0)
        remaining = self._scan_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
            return
        log.info(
            f"Scan | skip interval sleep — {elapsed:.1f}s >= {self._scan_interval}s"
        )

    def _reference_symbol(self) -> str | None:
        if not self._symbols:
            return None
        wanted = {s.upper() for s in self._symbols}
        refs = (
            ("BDO", "SM", "ICT", "ALI", "BPI")
            if self._market == "ph"
            else ("SPY", "AAPL", "MSFT", "QQQ")
        )
        for symbol in refs:
            if symbol in wanted:
                return next(s for s in self._symbols if s.upper() == symbol)
        return self._symbols[0]

    def _note_dead_symbol(self, symbol: str, reason: str) -> None:
        if symbol in self._dead_symbols:
            return
        self._dead_symbols.add(symbol)
        self.stats["dead_symbols"] = len(self._dead_symbols)
        log.warning(f"Scan | drop {symbol} for this run — {reason}")

    def _note_fetch_skip(self, symbol: str, exc: FetchSkip) -> None:
        if exc.code == "no_data":
            self._note_dead_symbol(symbol, str(exc))
        elif exc.code == "asof_mismatch":
            self.stats["asof_skipped"] += 1
        else:
            # history_unavailable and other transients: skip this cycle.
            self.stats["snapshot_errors"] += 1

    def _feed_worker_count(self) -> int:
        n = max(len(self._symbols), 1)
        conc = min(settings.scanner_concurrency, n)
        fetch_many = getattr(self._tv, "fetch_snapshots", None)
        batch = int(getattr(self._tv, "snapshot_batch_size", 1) or 1)
        if fetch_many is None or batch <= 1:
            return conc
        n_batches = max(1, (n + batch - 1) // batch)
        return min(conc, n_batches)

    async def _preload_feed(self, feed_sessions: list | None) -> None:
        preload = getattr(self._tv, "preload_universe", None)
        if preload is None:
            return
        session = feed_sessions[0] if feed_sessions else None
        try:
            summary = await preload(self._symbols, session)
        except Exception:
            log.exception("Scanner | universe preload failed — scan will lazy-load")
            return
        if summary:
            log.info(
                f"Scan | preloaded tapes loaded={summary.get('loaded')} "
                f"empty={summary.get('empty')} unavailable={summary.get('unavailable')}"
            )

    def _pin_candidates(self) -> list[str]:
        """Liquid names first; skip symbols already dropped as dead."""
        ordered: list[str] = []
        seen: set[str] = set()
        primary = self._reference_symbol()
        if primary:
            ordered.append(primary)
            seen.add(primary.upper())
        for symbol in self._symbols:
            key = symbol.upper()
            if key in seen or symbol in self._dead_symbols:
                continue
            ordered.append(symbol)
            seen.add(key)
            if len(ordered) >= 12:
                break
        return ordered

    async def _pin_replay_asof(self, feed_sessions: list | None) -> None:
        pin = getattr(self._tv, "pin_replay_asof", None)
        if pin is None or not feed_sessions:
            return
        for ref in self._pin_candidates():
            asof_day = await pin(ref, feed_sessions[0])
            if asof_day:
                log.info(f"Scan | replay asof pinned to {asof_day} via {ref}")
                return

    # ── Main async loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.start()
        n_workers = self._feed_worker_count()
        try:
            while self._running:
                try:
                    async with self._open_feed_sessions(n_workers) as sessions:
                        await self._preload_feed(sessions)
                        while self._running:
                            try:
                                if self._paper is not None:
                                    self._paper.tick()
                                await self._scan_all(feed_sessions=sessions)
                                if self._paper is not None:
                                    self._paper.save()
                                if self._stop_if_duration_reached():
                                    continue
                            except Exception:
                                # Broken MCP/stdio pipe: drop the pool and
                                # reopen rather than reuse a dead session.
                                log.exception(
                                    "Scanner | scan cycle failed — restarting data sessions"
                                )
                                break
                            await self._sleep_until_next_scan()
                except Exception:
                    log.exception(
                        "Scanner | failed to open data sessions — retrying next interval"
                    )
                    await asyncio.sleep(
                        self._scan_interval if self._scan_interval > 0 else 1.0
                    )
        finally:
            self.stop()

    @asynccontextmanager
    async def _open_feed_sessions(self, n: int):
        """Keep N MCP/stream sessions for the whole scan loop, not each cycle.

        Opening and killing tradingview-mcp on every scan races the SDK's
        killpg() against an already-exited child (ESRCH spam in the tqdm bar).
        """
        async with AsyncExitStack() as stack:
            sessions = [
                await stack.enter_async_context(self._tv.mcp_session())
                for _ in range(n)
            ]
            yield sessions

    # ── Scan cycle ─────────────────────────────────────────────────────────────
    async def _scan_all(self, feed_sessions: list | None = None) -> None:
        """Run one full scan across all symbols x timeframes x patterns.

        Symbols are processed concurrently with a progress bar. Each worker
        uses its own MCP session so there is no contention on the stdio pipe.
        """
        scan_start = time.monotonic()
        self._pending_kronos = []
        self._collect_pool = []
        self.stats["patterns_found"] = 0
        self.stats["signals_rejected"] = 0
        self.stats["trades_opened"] = 0
        self.stats["rejection_by_gate"] = {}

        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        fetch_many = getattr(self._tv, "fetch_snapshots", None)
        batch_size = 1
        if fetch_many is not None:
            batch_size = max(1, int(getattr(self._tv, "snapshot_batch_size", 1) or 1))
        concurrency = settings.scanner_concurrency
        log.info(
            f"Scan | {len(self._symbols)} symbols x {len(all_timeframes)} timeframes "
            f"({sorted(all_timeframes)}) x {len(self._patterns)} patterns | "
            f"concurrency={concurrency}"
            + (f" batch={batch_size}" if batch_size > 1 else "")
            + (
                f" analyze_pool={self._analyze_workers}"
                if self._analyze_pool is not None else " analyze=inline"
            )
        )

        # Latest detected signal per (symbol, timeframe) — unused for disk
        # charts (those render on dashboard click) but kept for vision checks.
        new_closed_daily = False
        self._scan_snapshot_symbols: set[str] = set()
        self._scan_daily_dates: set[str] = set()
        self.stats["asof_skipped"] = 0
        await self._pin_replay_asof(feed_sessions)

        from tqdm import tqdm

        # Fill a work queue with every symbol
        queue: asyncio.Queue[str] = asyncio.Queue()
        for s in self._symbols:
            queue.put_nowait(s)

        pbar = tqdm(total=len(self._symbols), desc="Scanning", unit="sym", ncols=80)

        def _take_batch() -> list[str]:
            batch: list[str] = []
            take = batch_size if fetch_many is not None else 1
            while len(batch) < take:
                try:
                    symbol = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if symbol in self._dead_symbols:
                    pbar.update(1)
                    continue
                batch.append(symbol)
            return batch

        async def _apply_result(symbol: str, timeframe: str, result) -> MarketSnapshot | None:
            nonlocal new_closed_daily
            self.stats["timeframe_requests"] += 1
            if isinstance(result, FetchSkip):
                self._note_fetch_skip(symbol, result)
                return None
            if result is None:
                self.stats["snapshot_errors"] += 1
                return None
            closed, pending = self._apply_bar(symbol, timeframe, result)
            if closed:
                new_closed_daily = True
            return pending

        async def _drain(mcp) -> None:
            while True:
                batch = _take_batch()
                if not batch:
                    return
                pending: list[MarketSnapshot] = []
                if fetch_many is not None:
                    for timeframe in all_timeframes:
                        try:
                            results = await fetch_many(
                                batch, timeframe,
                                store=self._store, mcp_session=mcp,
                            )
                        except Exception as exc:
                            self.stats["snapshot_errors"] += len(batch)
                            log.warning(f"Scan | batch snapshot failed: {exc}")
                            break
                        for symbol in batch:
                            snap = await _apply_result(
                                symbol, timeframe, (results or {}).get(symbol),
                            )
                            if snap is not None:
                                pending.append(snap)
                else:
                    for symbol in batch:
                        for timeframe in all_timeframes:
                            try:
                                snapshot = await self._tv.fetch_snapshot(
                                    symbol, timeframe,
                                    store=self._store, mcp_session=mcp,
                                )
                            except FetchSkip as exc:
                                self.stats["timeframe_requests"] += 1
                                self._note_fetch_skip(symbol, exc)
                                continue
                            except Exception as exc:
                                self.stats["timeframe_requests"] += 1
                                self.stats["snapshot_errors"] += 1
                                log.warning(
                                    f"Scan | snapshot failed {symbol} {timeframe}: {exc}"
                                )
                                continue
                            snap = await _apply_result(symbol, timeframe, snapshot)
                            if snap is not None:
                                pending.append(snap)
                if pending:
                    await self._run_analyzes(pending)
                pbar.update(len(batch))

        async def _owned_session_worker() -> None:
            async with self._tv.mcp_session() as mcp:
                await _drain(mcp)

        n_workers = min(self._feed_worker_count(), max(len(self._symbols), 1))
        if feed_sessions:
            workers = [_drain(mcp) for mcp in feed_sessions[:n_workers]]
        else:
            workers = [_owned_session_worker() for _ in range(n_workers)]
        try:
            await asyncio.gather(*workers)
        finally:
            pbar.close()

        if self._collect_first:
            await self._flush_collect_first()

        if self._kronos_batch and self._pending_kronos:
            await self._flush_kronos_batch()

        # Mark equity against the completed simulated bar BEFORE advancing the
        # replay. PaperAccount deduplicates marks to one point per market
        # session, so Sharpe is a daily/session statistic rather than a
        # function of the scanner's 60-second polling interval.
        if self._paper is not None:
            self._paper.mark_to_market(self._paper.sim_now())

        # Historical stream replay is advanced only after every symbol has
        # been scanned. A 1,000-symbol scan can take >60s, so a wall-clock
        # stream timer would otherwise make different symbols see different
        # simulated dates within the same scan.
        advance_replay = getattr(self._tv, "advance_replay", None)
        if advance_replay is not None:
            control_session = feed_sessions[0] if feed_sessions else None
            if not await advance_replay(control_session):
                log.warning("Scanner | paper replay did not advance after scan")

        if self._kronos_rank:
            if new_closed_daily:
                await self._run_kronos_rank_sleeve()
            else:
                log.debug(
                    "KronosRank | skip — no new closed daily bar this scan"
                )

        self.stats["symbols_with_snapshot"] = len(self._scan_snapshot_symbols)
        self.stats["daily_dates_seen"] = len(self._scan_daily_dates)
        self.stats["dead_symbols"] = len(self._dead_symbols)
        if len(self._scan_daily_dates) > 1:
            # The stream server is supposed to advance all loaded tapes
            # atomically. Missing/irregular symbol histories can still cause a
            # date skew; surface it rather than silently mixing market days.
            self.stats["daily_date_skew"] = (
                len(self._scan_daily_dates) - 1
            )
            log.warning(
                f"Scan | replay date skew detected: "
                f"daily dates={sorted(self._scan_daily_dates)}"
            )
        self.stats["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["scan_duration_s"] = round(time.monotonic() - scan_start, 2)
        log.info("Scan complete")

    def _apply_bar(self, symbol: str, timeframe: str, snapshot) -> tuple[bool, MarketSnapshot | None]:
        """Ledger + bar identity on the scan loop. Snapshot if patterns should run."""
        self.stats["timeframe_snapshots_ok"] += 1
        self._scan_snapshot_symbols.add(symbol)

        bar_key = (symbol, timeframe)
        bar_ts = snapshot.candle.timestamp
        identity = bar_identity(
            timeframe, bar_ts, market=self._market,
        )
        closed_bar = is_closed_session_bar(
            timeframe, bar_ts, market=self._market,
        )
        identity_key = repr(identity) if identity is not None else None
        persisted_key = self._persisted_bar_ids.get(f"{symbol}|{timeframe}")
        is_new_bar = (
            closed_bar
            and identity is not None
            and self._last_bar_ts.get(bar_key) != identity
            and identity_key != persisted_key
        )
        if identity is not None and closed_bar:
            self._last_bar_ts[bar_key] = identity
            if identity_key is not None and self._paper is not None:
                self._paper.mark_bar_processed(symbol, timeframe, identity_key)
                self._persisted_bar_ids[f"{symbol}|{timeframe}"] = identity_key
        new_closed_daily = False
        if is_new_bar and bar_ts is not None:
            self.stats["new_bars"] += 1
            if is_swing_timeframe(timeframe) and not is_weekly_timeframe(timeframe):
                local_ts = bar_ts.astimezone(
                    ZoneInfo(get_market(self._market).session_tz)
                )
                session_date = local_ts.strftime("%Y-%m-%d")
                self._scan_daily_dates.add(session_date)
                self._sim_day_keys.add(session_date)
                self._sim_days = len(self._sim_day_keys)
                self.stats["sim_days"] = self._sim_days
                new_closed_daily = True

        if self._paper is not None:
            closed = self._paper.on_bar(
                symbol, snapshot.candle, timeframe, is_new_bar,
            )
            if closed is not None:
                bar_idx = self._paper.bar_count(
                    closed.symbol, closed.timeframe,
                )
                self._cooldown_tracker[
                    (closed.symbol, closed.pattern)
                ] = (bar_idx, closed.pnl < 0)

        if not is_new_bar:
            return new_closed_daily, None

        pending = self._pending_entries.pop((symbol, timeframe), None)
        if pending is not None and self._paper is not None:
            opened, fill_reason = self._paper.open_position(
                pending, snapshot.candle, self._store
            )
            if opened:
                self.stats["trades_opened"] += 1
                self._append_signal_log(
                    pending, status="filled", reason=fill_reason,
                )
            elif fill_reason.startswith("Session "):
                self._pending_entries[(symbol, timeframe)] = pending
            else:
                self.stats["signals_rejected"] += 1
                self._append_signal_log(
                    pending, status="rejected", reason=fill_reason,
                )

        if self._has_ready_pattern(symbol, timeframe):
            return new_closed_daily, snapshot
        return new_closed_daily, None

    def _has_ready_pattern(self, symbol: str, timeframe: str) -> bool:
        n_bars = self._store.available(symbol, timeframe)
        logged = False
        ready = False
        for pattern in self._patterns:
            if timeframe not in pattern.timeframes:
                continue
            min_bars = max(
                int(getattr(pattern, "MIN_BARS", 2) or 2),
                PATTERN_SCAN_HISTORY_BARS,
            )
            if n_bars < min_bars:
                if (
                    not logged
                    and (symbol, timeframe) not in self._thin_logged
                ):
                    self._thin_logged.add((symbol, timeframe))
                    log.debug(
                        f"Scan | {symbol} {timeframe} has {n_bars} bars "
                        f"(need {min_bars}) — skip patterns this run"
                    )
                    logged = True
                continue
            ready = True
        return ready

    def _analyze_local(self, snapshot: MarketSnapshot) -> tuple[int, list[TradeSignal]]:
        """Inline analyze on this loop — tests, workers=1, or pool fallback."""
        symbol, timeframe = snapshot.symbol, snapshot.timeframe
        n_bars = self._store.available(symbol, timeframe)
        n_eval = 0
        hits: list[TradeSignal] = []
        for pattern in self._patterns:
            if timeframe not in pattern.timeframes:
                continue
            min_bars = max(
                int(getattr(pattern, "MIN_BARS", 2) or 2),
                PATTERN_SCAN_HISTORY_BARS,
            )
            if n_bars < min_bars:
                continue
            n_eval += 1
            try:
                signal = pattern.analyze(snapshot, self._store)
            except Exception:
                log.exception(
                    f"Scan | {pattern.name} {symbol} {timeframe}"
                )
                continue
            if signal:
                hits.append(signal)
        return n_eval, hits

    async def _emit_signals(
        self, snapshot: MarketSnapshot, signals: list[TradeSignal],
    ) -> None:
        by_name = {p.name: p for p in self._patterns}
        for signal in signals:
            self.stats["patterns_found"] += 1
            self._record_detection(signal)
            await self._process_signal(
                signal, by_name.get(signal.pattern), snapshot.candle,
            )

    async def _run_analyzes(self, snapshots: list[MarketSnapshot]) -> None:
        """Run pattern.analyze() off-loop when a spawn pool is open."""
        if not snapshots:
            return
        if self._analyze_pool is None:
            for snapshot in snapshots:
                n_eval, hits = self._analyze_local(snapshot)
                self.stats["pattern_evaluations"] += n_eval
                await self._emit_signals(snapshot, hits)
            return

        from core.pattern_jobs import analyze_batch

        jobs = [
            (snapshot, self._store.copy_candles(snapshot.symbol, snapshot.timeframe))
            for snapshot in snapshots
        ]
        n = max(1, int(self._analyze_workers) or 1)
        chunk_size = max(4, min(16, (len(jobs) + n - 1) // n))
        chunks = [jobs[i:i + chunk_size] for i in range(0, len(jobs), chunk_size)]
        loop = asyncio.get_running_loop()
        try:
            parts = await asyncio.gather(*[
                loop.run_in_executor(self._analyze_pool, analyze_batch, chunk)
                for chunk in chunks
            ])
        except Exception:
            log.exception("Scan | analyze pool failed — inline this batch")
            for snapshot in snapshots:
                n_eval, hits = self._analyze_local(snapshot)
                self.stats["pattern_evaluations"] += n_eval
                await self._emit_signals(snapshot, hits)
            return
        flat: list[tuple[int, list[TradeSignal]]] = []
        for part in parts:
            flat.extend(part)
        for snapshot, (n_eval, signals) in zip(snapshots, flat):
            self.stats["pattern_evaluations"] += n_eval
            await self._emit_signals(snapshot, signals)

    async def _run_kronos_rank_sleeve(self) -> None:
        """Cross-sectional top-K forecast sleeve — runs once per new daily asof."""
        # Pick a representative asof from any symbol with 1d history.
        asof = None
        for symbol in self._symbols:
            df = self._store.get_df(symbol, "1d", min_bars=1)
            if df is not None and len(df):
                asof = pd.Timestamp(df.index[-1]).normalize()
                break
        if asof is None:
            return
        if self._kronos_rank_last_asof == asof:
            log.debug(f"KronosRank | skip — already forecast asof={asof.date()}")
            return

        signals = await asyncio.to_thread(
            run_sleeve,
            self._store,
            list(self._symbols),
            long_only=(
                False
                if not structure_filters_enabled(self._pattern_only)
                else (True if get_market(self._market).long_only else None)
            ),
            use_batch=self._kronos_batch,
        )
        self._kronos_rank_last_asof = asof
        self.stats["kronos_rank_emitted"] = self.stats.get("kronos_rank_emitted", 0) + len(
            signals
        )
        for signal in signals:
            self.stats["patterns_found"] += 1
            self._record_detection(signal)
            candle = self._store.latest_candle(signal.symbol, "1d")
            await self._process_signal(signal, pattern=None, candle=candle)

    # ── Signal pipeline ────────────────────────────────────────────────────────
    async def _process_signal(
        self, signal: TradeSignal, pattern: BasePattern | None = None, candle=None,
    ) -> None:
        log.info(
            f"Signal | {signal.symbol} {signal.timeframe} | "
            f"{signal.action} | pattern={signal.pattern} | "
            f"confidence={signal.confidence:.2f}"
        )
        # Stamp the signal event before any gate can reject it, so the Logs
        # tab can correlate both accepted and rejected decisions to the
        # simulated market bar during replay.
        if candle is not None:
            signal.signal_bar_idx = (
                self._paper.bar_count(signal.symbol, signal.timeframe)
                if self._paper is not None else None
            )
            signal.signal_bar_timestamp = candle.timestamp

        # Execution risk gates — enforced regardless of Pattern-only.
        # Pattern-only exists to isolate raw pattern edge from *signal-quality*
        # filters (confidence / regime / long-only) below, but the min-price
        # floor and post-loss cooldown are basic account-risk controls, not
        # pattern-quality opinions: skipping them let real (paper) money into
        # sub-$1 names with outsized slippage risk (2026-08-30 review: a
        # $0.003 stock lost -10.3% against a stop sized for ~6%) and allowed
        # same-session re-entry into a name that had just stopped out. Both
        # now apply even when Pattern-only is checked.
        price_reason = describe_min_share_price_rejection(
            signal, self._min_share_price, market=self._market,
        )
        if price_reason is not None:
            log.info(
                f"Signal REJECTED by min share price — {signal.symbol} "
                f"{signal.pattern} | {price_reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=price_reason)
            return

        bar_idx = (
            self._paper.bar_count(signal.symbol, signal.timeframe)
            if self._paper is not None else 0
        )
        cooldown_reason = describe_cooldown_rejection(
            signal, bar_idx, self._cooldown_tracker,
        )
        if cooldown_reason is not None:
            log.info(
                f"Signal REJECTED by cooldown — {signal.symbol} "
                f"{signal.pattern} | {cooldown_reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=cooldown_reason)
            return

        # Step 0 — Same entry gates the backtester applies before Kronos/volume
        # (min_confidence + SMA200 regime). Without these, paper/live took
        # trades the "validated" backtest would have skipped. Pattern-only
        # skips confidence for all patterns, but the regime gate stays on
        # for REGIME_REQUIRED_PATTERNS (006/007) even in Pattern-only — see
        # regime_filter_required() / REGIME_REQUIRED_PATTERNS.
        if structure_filters_enabled(self._pattern_only):
            if not passes_min_confidence(signal):
                reason = describe_confidence_rejection(signal)
                log.info(
                    f"Signal REJECTED by confidence — {signal.symbol} "
                    f"{signal.pattern} | {reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        if regime_filter_required(signal.pattern, self._pattern_only):
            regime_reason = describe_regime_rejection(
                signal, self._store, market=self._market,
            )
            if regime_reason is not None:
                log.info(
                    f"Signal REJECTED by regime filter — {signal.symbol} "
                    f"{signal.pattern} | {regime_reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=regime_reason)
                return

        if structure_filters_enabled(self._pattern_only):
            profile = get_market(self._market)
            if profile.long_only and signal.action == "SELL":
                reason = (
                    f"Long-only {profile.label}: pattern SELL/short is disabled "
                    f"(PSE retail shorts need SBL)."
                )
                log.info(
                    f"Signal REJECTED by long-only — {signal.symbol} "
                    f"{signal.pattern} | {reason}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        risk_reason = describe_risk_gate_rejection(
            signal, self._store, signal.symbol, signal.timeframe,
            **risk_gate_kwargs(),
        )
        if risk_reason is not None:
            log.info(
                f"Signal REJECTED by risk gates — {signal.symbol} "
                f"{signal.pattern} | {risk_reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=risk_reason)
            return

        # Collect-first: don't finish yet. Defer Kronos/volume/vision until the
        # full scan has drained, then rank the pool by R:R and finish only the
        # top-N. The Kronos rank sleeve keeps its own top-K path (skipped here).
        if self._collect_first and not is_kronos_rank_signal(signal):
            self._collect_pool.append((signal, pattern, candle))
            return

        await self._kronos_then_finish(signal, pattern, candle)

    async def _kronos_then_finish(
        self, signal: TradeSignal, pattern: BasePattern | None = None, candle=None,
    ) -> None:
        # Step 0b — Kronos 3d confirm gate. Sequential unless Batch Kronos.
        # Skip for pattern_kronos_rank — the forecast *is* the entry signal.
        if self._kronos_gate and not is_kronos_rank_signal(signal):
            if self._kronos_batch:
                async with self._pending_kronos_lock:
                    self._pending_kronos.append((signal, pattern, candle))
                return
            gate = kronos_gate_check(signal, self._store)
            if not self._kronos_gate_ok(signal, gate):
                return

        await self._finish_signal(signal, pattern, candle)

    def _kronos_gate_ok(self, signal: TradeSignal, gate) -> bool:
        if not gate.passed:
            reason = (
                f"Kronos 3d confirm gate vetoed this {signal.action}: {gate.reason}. "
                f"Forecast must agree with the pattern direction and clear "
                f"3% in 3 days (KRONOS_MIN_MOVE_PCT)."
            )
            log.info(
                f"Signal REJECTED by Kronos gate — {signal.symbol} "
                f"{signal.pattern} | {gate.reason}"
            )
            self.stats["signals_rejected"] += 1
            self._append_signal_log(signal, status="rejected", reason=reason)
            return False
        if gate.pred_1w is not None:
            log.info(
                f"Kronos gate PASS | {signal.symbol} {signal.pattern} | "
                f"pred_3d={gate.pred_1w:+.2%} in 3d | {gate.reason}"
            )
        return True

    async def _flush_collect_first(self) -> None:
        """Rank the collected chart-pattern signals by R:R; finish the top-N."""
        if not self._collect_first or not self._collect_pool:
            return
        pool = self._collect_pool
        self._collect_pool = []

        # One entry per (symbol, timeframe) — keep the highest R:R signal.
        best: dict[tuple[str, str], tuple[TradeSignal, BasePattern | None, object]] = {}
        for signal, pattern, candle in pool:
            key = (signal.symbol, signal.timeframe)
            rr = signal_reward_risk(signal)
            existing = best.get(key)
            if existing is None:
                best[key] = (signal, pattern, candle)
                continue
            existing_rr = signal_reward_risk(existing[0])
            if rr is not None and (existing_rr is None or rr > existing_rr):
                best[key] = (signal, pattern, candle)

        def _sort_key(item):
            rr = signal_reward_risk(item[0])
            return (
                rr is None,
                -(rr if rr is not None else 0.0),
                -float(item[0].confidence),
                item[0].symbol,
            )

        ranked = sorted(best.values(), key=_sort_key)
        selected = ranked[: self._collect_first_top_n]
        rejected = ranked[self._collect_first_top_n :]

        self.stats["collect_first_ranked"] = self.stats.get("collect_first_ranked", 0) + len(ranked)
        self.stats["collect_first_selected"] = self.stats.get("collect_first_selected", 0) + len(selected)

        log.info(
            f"Collect-first | {len(ranked)} ranked by R:R, "
            f"selected top {len(selected)} of {self._collect_first_top_n}"
        )

        for signal, _pattern, _candle in rejected:
            self.stats["signals_rejected"] += 1
            self._append_signal_log(
                signal,
                status="rejected",
                reason=(
                    f"Collect-first: R:R ranked below top "
                    f"{self._collect_first_top_n} this scan."
                ),
            )

        for signal, pattern, candle in selected:
            log.info(
                f"Collect-first | selected {signal.symbol} {signal.pattern} "
                f"R:R={signal_reward_risk(signal):.2f}"
            )
            await self._kronos_then_finish(signal, pattern, candle)

    async def _flush_kronos_batch(self) -> None:
        pending = self._pending_kronos
        self._pending_kronos = []
        if not pending:
            return
        signals = [item[0] for item in pending]
        log.info(f"Kronos batch | gating {len(signals)} pattern hits")
        results = await asyncio.to_thread(
            kronos_gate_check_many, signals, self._store,
        )
        for (signal, pattern, candle), gate in zip(pending, results):
            if not self._kronos_gate_ok(signal, gate):
                continue
            await self._finish_signal(signal, pattern, candle)

    async def _finish_signal(
        self, signal: TradeSignal, pattern: BasePattern | None = None, candle=None,
    ) -> None:
        # Sleeve skips volume by default — ranking is price-path based.
        if self._volume_gate and not is_kronos_rank_signal(signal):
            vgate = volume_confirm_gate(signal, self._store)
            if not vgate.passed:
                reason = (
                    f"Volume confirm gate vetoed this {signal.action}: {vgate.reason}. "
                    f"Needs relative volume ≥ VOLUME_GATE_RVOL_MIN and OBV slope "
                    f"agreeing with the trade direction."
                )
                log.info(
                    f"Volume gate REJECT | {signal.symbol} {signal.pattern} | "
                    f"{vgate.reason}"
                )
                self.stats["signals_rejected"] += 1
                self.stats["volume_gate_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            log.info(
                f"Volume gate PASS | {signal.symbol} {signal.pattern} | "
                f"{vgate.reason}"
            )

        # Step 1 — Vision confirmation, only when enabled. The pattern's own
        # min_confidence already gated this signal before analyze() returned
        # it — vision_min_indicator_confidence exists only to decide whether
        # a signal is worth spending a vision check on, not as a second
        # trade-approval gate when vision is off.
        # Skip vision for Kronos rank sleeve (no chart pattern to confirm).
        if settings.vision_confirmation_enabled and not is_kronos_rank_signal(signal):
            if signal.confidence < settings.vision_min_indicator_confidence:
                reason = (
                    f"Vision gate skipped the trade: confidence {signal.confidence:.2f} "
                    f"is below VISION_MIN_INDICATOR_CONFIDENCE "
                    f"({settings.vision_min_indicator_confidence:.2f}), so no Claude "
                    f"check was run and the signal was dropped."
                )
                log.info(
                    f"Signal confidence {signal.confidence:.2f} below threshold "
                    f"{settings.vision_min_indicator_confidence} — skipping vision, skipping trade"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            if pattern is None:
                reason = "Vision confirmation requires a pattern instance — none provided."
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return
            verdict = await self._run_vision_check(signal, pattern)
            if verdict != VisionVerdict.CONFIRM:
                reason = (
                    f"Vision confirmation failed with verdict {verdict} — Claude did "
                    f"not CONFIRM the {signal.pattern} chart setup on {signal.symbol}."
                )
                log.info(
                    f"Signal REJECTED by vision check "
                    f"({verdict}) — {signal.symbol} {signal.pattern}"
                )
                self.stats["signals_rejected"] += 1
                self._append_signal_log(signal, status="rejected", reason=reason)
                return

        # Step 3 — Place the order (disabled while IBKR is commented out).
        # Not filled here: queued to fill on this symbol's *next* new bar,
        # same one-bar deferral the backtester's pending_entry uses — see
        # self._pending_entries.
        if self._paper is not None and candle is not None:
            # The signal bar is one bar before the deferred fill. Carry its
            # identity into the position so event-based time stops (notably
            # neckline/channel exits) start from the breakout bar.
            self._pending_entries[(signal.symbol, signal.timeframe)] = signal
            extras = (
                f"{', Kronos' if self._kronos_gate else ''}"
                f"{', volume' if self._volume_gate else ''}"
            )
            if self._pattern_only:
                accept_reason = (
                    f"Pattern-only: structure filters skipped "
                    f"(confidence {signal.confidence:.2f}{extras}). "
                    f"Queued to fill on the next new {signal.timeframe} bar "
                    f"close — same one-bar deferral as the backtester."
                )
            else:
                accept_reason = (
                    f"Cleared entry gates (confidence {signal.confidence:.2f}, "
                    f"SMA200 regime, cooldown{extras}). "
                    f"Queued to fill on the next new {signal.timeframe} bar "
                    f"close — same one-bar deferral as the backtester."
                )
            self._append_signal_log(
                signal,
                status="accepted",
                reason=accept_reason,
            )
        else:
            log.info(
                f"Signal APPROVED (IBKR disabled) — would {signal.action} "
                f"{signal.qty} {signal.symbol} @ ~{signal.price:.2f}"
            )
            self._append_signal_log(
                signal,
                status="accepted",
                reason=(
                    f"Cleared entry gates; IBKR execution disabled so this would "
                    f"{signal.action} {signal.qty:g} {signal.symbol} @ ~{signal.price:.2f} "
                    f"but no order was sent."
                ),
            )
        # if signal.action == "BUY":
        #     self._orders.place_market_order(
        #         signal.symbol, "BUY", signal.qty, signal.pattern
        #     )
        # elif signal.action == "SELL":
        #     self._orders.place_market_order(
        #         signal.symbol, "SELL", signal.qty, signal.pattern
        #     )
        # elif signal.action == "CLOSE":
        #     self._orders.close_position(signal.symbol, signal.qty, signal.pattern)

    def ohlcv_frame(self, symbol: str, timeframe: str, min_bars: int = 2):
        """OHLCV for on-demand chart rendering (open/closed row click)."""
        return self._store.get_df(symbol, timeframe, min_bars=min_bars)

    async def _run_vision_check(
        self, signal: TradeSignal, pattern: BasePattern
    ) -> VisionVerdict:
        df = self._store.get_df(signal.symbol, signal.timeframe, min_bars=2)
        if df is None:
            log.warning("Vision | No OHLCV data in store — skipping visual check")
            return VisionVerdict.UNCERTAIN

        chart_png = self._renderer.render(
            signal.symbol, signal.timeframe, df,
            annotations=signal.chart_annotations or None,
        )
        return self._vision.check(
            chart_png=chart_png,
            pattern_name=pattern.name,
            pattern_description=pattern.chart_description,
            symbol=signal.symbol,
            action=signal.action,
        )

    # ── Pattern discovery ──────────────────────────────────────────────────────
    _DETECTED_TABLE_HEADER = (
        "| timestamp | pattern | file | symbol | timeframe | action | confidence |\n"
        "|---|---|---|---|---|---|---|\n"
    )

    def _init_patterns_detected_file(self) -> None:
        PATTERNS_DETECTED_FILE.write_text(
            "# Pattern detections\n\n" + self._DETECTED_TABLE_HEADER,
            encoding="utf-8",
        )

    def _record_detection(self, signal: TradeSignal) -> None:
        if signal.pattern in EXCLUDED_PATTERNS:
            return
        filename = self._pattern_files.get(signal.pattern, "?")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (
            f"| {ts} | `{signal.pattern}` | {filename} | "
            f"**{signal.symbol}** | {signal.timeframe} | "
            f"{signal.action} | {signal.confidence:.2f} |\n"
        )
        with PATTERNS_DETECTED_FILE.open("a", encoding="utf-8") as f:
            f.write(row)

    def _discover_patterns(self) -> None:
        for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
            if skip_pattern_module(module_info.name):
                continue
            module = importlib.import_module(f"patterns.{module_info.name}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BasePattern)
                    and attr is not BasePattern
                ):
                    instance = attr()
                    if instance.skipped or instance.name in self._disabled_patterns:
                        continue
                    self._patterns.append(instance)
                    self._pattern_files[instance.name] = (
                        f"patterns/{module_info.name}.py"
                    )
                    log.info(f"Scanner | Registered pattern: {instance}")
