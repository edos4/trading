"""
core/scanner.py — The market scanner. Replaces both the trading engine
and the webhook server from the previous architecture.

Every SCAN_INTERVAL_SECONDS it:
  1. Fetches fresh data from TradingView MCP for every (symbol, timeframe) pair
  2. Pushes the new candle into OHLCVStore
  3. Runs each registered pattern's analyze() method
  4. If a signal is returned:
        a. Kronos 1w forecast gate (if enabled) — direction + min move
        b. Renders a chart (if vision is enabled)
        c. Asks Claude vision to confirm the pattern
        d. If confirmed: runs risk checks → places order

Concurrency: symbols are processed in parallel across N MCP sessions
(one session per worker, controlled by scanner_concurrency setting).
A tqdm progress bar shows scan progress on the CLI.
"""

from __future__ import annotations
import asyncio
import importlib
import pkgutil
import time
from datetime import datetime, timezone
from pathlib import Path

import patterns as patterns_pkg
from patterns.base_pattern import BasePattern, TradeSignal

from data.tv_client import TVClient, MarketSnapshot
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from analysis.chart_renderer import ChartRenderer
from analysis.vision_checker import VisionChecker, VisionVerdict
from core.paper_trader import PaperAccount
from core.kronos_gate import kronos_gate_check

# TODO: re-enable IBKR when TWS/Gateway is available
# from broker.ibkr_client import IBKRClient
# from broker.order_manager import OrderManager
# from risk.risk_guard import RiskGuard, TradeIntent
from config import settings
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
    ):
        self._symbols = symbols or settings.symbols
        self._disabled_patterns = set(disabled_patterns or [])
        self._scan_interval = scan_interval_seconds or settings.scan_interval_seconds
        # None → follow settings; explicit True/False lets UI/CLI override for a session.
        self._kronos_gate = (
            settings.kronos_gate_enabled if kronos_gate is None else kronos_gate
        )
        self._tv = data_feed or TVClient(
            settings.tv_screener,
            settings.tv_exchange,
            exchange_overrides=exchange_overrides,
        )
        self._store = OHLCVStore(window=max(DEFAULT_WINDOW, settings.tv_history_days))
        self._renderer = ChartRenderer(save_to_disk=True)
        self._vision = VisionChecker()
        # self._client   = IBKRClient()
        # self._orders   = OrderManager(self._client)
        # self._risk     = RiskGuard(self._client)
        self._paper = paper_account
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._running = False
        # Last bar timestamp seen per (symbol, timeframe) — a daily-timeframe
        # pattern scanned hourly would otherwise re-detect the exact same
        # unchanged bar every cycle and repeatedly open/close identical trades.
        self._last_bar_ts: dict[tuple[str, str], datetime] = {}
        # New-bar count per symbol this run — the paper trade stream server
        # advances each symbol's tape by exactly one row (= one day) per
        # scan tick, so counting new bars is counting simulated days.
        self._sim_ticks: dict[str, int] = {}
        self._sim_days: int = 0
        # Signal detected on bar i, filled on bar i+1's close — mirrors the
        # backtester's pending_entry deferral (core/backtester.py) so paper/
        # live trading isn't more optimistic than the backtest that validated
        # the strategy (filling on the very candle whose close triggered it).
        self._pending_entries: dict[str, TradeSignal] = {}
        # Scan-cycle health counters — surfaced by the paper trading UI/CLI
        # so a stalled or misbehaving scan is visible without reading logs.
        self.stats: dict = {
            "last_scan_at": None,
            "scan_duration_s": 0.0,
            "patterns_found": 0,
            "signals_rejected": 0,
            "trades_opened": 0,
            "sim_days": 0,
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        # self._client.connect()
        self._discover_patterns()
        self._init_patterns_detected_file()
        for p in self._patterns:
            p.on_start()
        self._running = True
        log.info(
            f"Scanner started | "
            f"symbols={self._symbols} | "
            f"patterns={[p.name for p in self._patterns]} | "
            f"kronos_gate={'ON' if self._kronos_gate else 'OFF'} | "
            f"interval={self._scan_interval}s"
        )

    def stop(self) -> None:
        self._running = False
        for p in self._patterns:
            p.on_stop()
        # self._client.disconnect()
        log.info("Scanner stopped")

    # ── Main async loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.start()
        try:
            while self._running:
                try:
                    if self._paper is not None:
                        self._paper.tick()
                    await self._scan_all()
                    if self._paper is not None:
                        self._paper.save()
                except Exception:
                    # A single bad scan cycle (MCP subprocess crash, broken
                    # pipe, transient TradingView error) must not take down
                    # a session meant to run unattended for days.
                    log.exception("Scanner | scan cycle failed — retrying next interval")
                await asyncio.sleep(self._scan_interval)
        finally:
            self.stop()

    # ── Scan cycle ─────────────────────────────────────────────────────────────
    async def _scan_all(self) -> None:
        """Run one full scan across all symbols x timeframes x patterns.

        Symbols are processed concurrently with a progress bar. Each worker
        opens its own MCP session so there is no contention on the stdio pipe.
        """
        scan_start = time.monotonic()
        self.stats["patterns_found"] = 0
        self.stats["signals_rejected"] = 0
        self.stats["trades_opened"] = 0

        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        concurrency = settings.scanner_concurrency
        log.info(
            f"Scan | {len(self._symbols)} symbols x {len(all_timeframes)} timeframes "
            f"({sorted(all_timeframes)}) x {len(self._patterns)} patterns | "
            f"concurrency={concurrency}"
        )

        # Latest detected signal per (symbol, timeframe) — its annotations are
        # drawn on the post-scan chart PNG so the pattern is easy to eyeball.
        latest_signals: dict[tuple[str, str], TradeSignal] = {}

        from tqdm import tqdm

        # Fill a work queue with every symbol
        queue: asyncio.Queue[str] = asyncio.Queue()
        for s in self._symbols:
            queue.put_nowait(s)

        pbar = tqdm(total=len(self._symbols), desc="Scanning", unit="sym", ncols=80)

        async def _worker() -> None:
            """Each worker owns one MCP session for its entire lifetime."""
            async with self._tv.mcp_session() as mcp:
                while True:
                    try:
                        symbol = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return

                    for timeframe in all_timeframes:
                        snapshot = await self._tv.fetch_snapshot(
                            symbol, timeframe,
                            store=self._store, mcp_session=mcp,
                        )
                        if snapshot is None:
                            continue

                        bar_key = (symbol, timeframe)
                        bar_ts = snapshot.candle.timestamp
                        is_new_bar = bar_ts is None or self._last_bar_ts.get(bar_key) != bar_ts
                        if bar_ts is not None:
                            self._last_bar_ts[bar_key] = bar_ts
                        if is_new_bar and bar_ts is not None:
                            ticks = self._sim_ticks.get(symbol, 0) + 1
                            self._sim_ticks[symbol] = ticks
                            self._sim_days = max(self._sim_days, ticks)
                            self.stats["sim_days"] = self._sim_days

                        if self._paper is not None:
                            self._paper.on_bar(symbol, snapshot.candle, timeframe, is_new_bar)

                        if not is_new_bar:
                            # Same bar as last scan (e.g. a daily pattern
                            # polled hourly, market closed/quiet) — re-running
                            # pattern detection on unchanged data would just
                            # re-fire the same signal and reopen/close the
                            # same trade every cycle.
                            continue

                        pending = self._pending_entries.pop(symbol, None)
                        if pending is not None and self._paper is not None:
                            opened = self._paper.open_position(
                                pending, snapshot.candle, self._store
                            )
                            if opened:
                                self.stats["trades_opened"] += 1
                            else:
                                self.stats["signals_rejected"] += 1

                        for pattern in self._patterns:
                            if timeframe not in pattern.timeframes:
                                continue
                            signal = pattern.analyze(snapshot, self._store)
                            if signal:
                                self.stats["patterns_found"] += 1
                                self._record_detection(signal)
                                latest_signals[(symbol, timeframe)] = signal
                                await self._process_signal(signal, pattern, snapshot.candle)

                    pbar.update(1)

        workers = [_worker() for _ in range(min(concurrency, len(self._symbols)))]
        await asyncio.gather(*workers)
        pbar.close()

        self._save_scan_charts(all_timeframes, latest_signals)
        self.stats["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["scan_duration_s"] = round(time.monotonic() - scan_start, 2)
        log.info("Scan complete")

    # ── Signal pipeline ────────────────────────────────────────────────────────
    async def _process_signal(
        self, signal: TradeSignal, pattern: BasePattern, candle=None,
    ) -> None:
        log.info(
            f"Signal | {signal.symbol} {signal.timeframe} | "
            f"{signal.action} | pattern={signal.pattern} | "
            f"confidence={signal.confidence:.2f}"
        )

        # Step 0 — Kronos 1w confirm gate (direction + min move). Runs before
        # vision so we don't burn Claude tokens on forecasts that disagree.
        # Fail-open when weights missing — see core/kronos_gate.py.
        if self._kronos_gate:
            gate = kronos_gate_check(signal, self._store)
            if not gate.passed:
                log.info(
                    f"Signal REJECTED by Kronos gate — {signal.symbol} "
                    f"{signal.pattern} | {gate.reason}"
                )
                self.stats["signals_rejected"] += 1
                return
            if gate.pred_1w is not None:
                log.info(
                    f"Kronos gate PASS | {signal.symbol} {signal.pattern} | "
                    f"pred_1w={gate.pred_1w:+.2%} | {gate.reason}"
                )

        # Step 1 — Vision confirmation, only when enabled. The pattern's own
        # min_confidence already gated this signal before analyze() returned
        # it — vision_min_indicator_confidence exists only to decide whether
        # a signal is worth spending a vision check on, not as a second
        # trade-approval gate when vision is off.
        if settings.vision_confirmation_enabled:
            if signal.confidence < settings.vision_min_indicator_confidence:
                log.info(
                    f"Signal confidence {signal.confidence:.2f} below threshold "
                    f"{settings.vision_min_indicator_confidence} — skipping vision, skipping trade"
                )
                self.stats["signals_rejected"] += 1
                return
            verdict = await self._run_vision_check(signal, pattern)
            if verdict != VisionVerdict.CONFIRM:
                log.info(
                    f"Signal REJECTED by vision check "
                    f"({verdict}) — {signal.symbol} {signal.pattern}"
                )
                self.stats["signals_rejected"] += 1
                return

        # Step 2 — Risk guard (disabled while IBKR is commented out)
        # intent = TradeIntent(
        #     symbol=signal.symbol,
        #     action=signal.action,
        #     qty=signal.qty,
        #     estimated_price=signal.price,
        #     pattern=signal.pattern,
        # )
        # if not self._risk.approve(intent):
        #     return    # risk_guard already logged the block reason

        # Step 3 — Place the order (disabled while IBKR is commented out).
        # Not filled here: queued to fill on this symbol's *next* new bar,
        # same one-bar deferral the backtester's pending_entry uses — see
        # self._pending_entries.
        if self._paper is not None and candle is not None:
            self._pending_entries[signal.symbol] = signal
        else:
            log.info(
                f"Signal APPROVED (IBKR disabled) — would {signal.action} "
                f"{signal.qty} {signal.symbol} @ ~{signal.price:.2f}"
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

    def _save_scan_charts(
        self,
        timeframes: set[str],
        latest_signals: dict[tuple[str, str], TradeSignal] | None = None,
    ) -> None:
        """Write PNG charts for symbols that had a signal detected this scan.

        Annotations are drawn on the PNG so the setup is easy to see/check.
        Previously this rendered every symbol, but with thousands of symbols
        that is no longer practical — only symbols with active signals get charts.
        """
        latest_signals = latest_signals or {}
        chart_timeframes = {tf for tf in timeframes if tf != "1W"}
        items = [
            (symbol, tf, sig)
            for (symbol, tf), sig in latest_signals.items()
            if tf in chart_timeframes
        ]
        if not items:
            return

        from tqdm import tqdm

        for symbol, timeframe, signal in tqdm(
            items, desc="Saving charts", unit="chart", ncols=80
        ):
            df = self._store.get_df(symbol, timeframe, min_bars=1)
            if df is None:
                continue
            try:
                self._renderer.render_with_ema(
                    symbol, timeframe, df,
                    annotations=signal.chart_annotations if signal else None,
                )
            except Exception as exc:
                log.warning(
                    f"Scanner | Chart render failed for {symbol} {timeframe}: {exc}"
                )

    async def _run_vision_check(
        self, signal: TradeSignal, pattern: BasePattern
    ) -> VisionVerdict:
        df = self._store.get_df(signal.symbol, signal.timeframe, min_bars=2)
        if df is None:
            log.warning("Vision | No OHLCV data in store — skipping visual check")
            return VisionVerdict.UNCERTAIN

        chart_png = self._renderer.render_with_ema(
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
            if module_info.name.startswith("_") or module_info.name == "base_pattern":
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
