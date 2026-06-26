"""
core/scanner.py — The market scanner. Replaces both the trading engine
and the webhook server from the previous architecture.

Every SCAN_INTERVAL_SECONDS it:
  1. Fetches fresh data from TradingView MCP for every (symbol, timeframe) pair
  2. Pushes the new candle into OHLCVStore
  3. Runs each registered pattern's analyze() method
  4. If a signal is returned:
       a. Checks confidence threshold
       b. Renders a chart (if vision is enabled)
       c. Asks Claude vision to confirm the pattern
       d. If confirmed: runs risk checks → places order
"""

from __future__ import annotations
import asyncio
import importlib
import pkgutil
from datetime import datetime, timezone
from pathlib import Path

import patterns as patterns_pkg
from patterns.base_pattern import BasePattern, TradeSignal

from data.tv_client import TVClient, MarketSnapshot
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from analysis.chart_renderer import ChartRenderer
from analysis.vision_checker import VisionChecker, VisionVerdict
# TODO: re-enable IBKR when TWS/Gateway is available
# from broker.ibkr_client import IBKRClient
# from broker.order_manager import OrderManager
# from risk.risk_guard import RiskGuard, TradeIntent
from config import settings
from utils.logger import log

PATTERNS_DETECTED_FILE = Path("patterns_detected.txt")
EXCLUDED_PATTERNS = {"pattern_001_ema_crossover"}


class MarketScanner:
    def __init__(self):
        self._tv       = TVClient(settings.tv_screener, settings.tv_exchange)
        self._store    = OHLCVStore(window=max(DEFAULT_WINDOW, settings.tv_history_days))
        self._renderer = ChartRenderer(save_to_disk=True)
        self._vision   = VisionChecker()
        # self._client   = IBKRClient()
        # self._orders   = OrderManager(self._client)
        # self._risk     = RiskGuard(self._client)
        self._patterns: list[BasePattern] = []
        self._pattern_files: dict[str, str] = {}
        self._running  = False

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
            f"symbols={settings.symbols} | "
            f"patterns={[p.name for p in self._patterns]} | "
            f"interval={settings.scan_interval_seconds}s"
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
                await self._scan_all()
                await asyncio.sleep(settings.scan_interval_seconds)
        finally:
            self.stop()

    # ── Scan cycle ─────────────────────────────────────────────────────────────
    async def _scan_all(self) -> None:
        """Run one full scan across all symbols × timeframes × patterns."""
        # Collect all unique timeframes any pattern wants
        all_timeframes: set[str] = set()
        for p in self._patterns:
            all_timeframes.update(p.timeframes)

        async with self._tv.mcp_session() as mcp:
            for symbol in settings.symbols:
                for timeframe in all_timeframes:
                    snapshot = await self._tv.fetch_snapshot(
                        symbol, timeframe, store=self._store, mcp_session=mcp
                    )
                    if snapshot is None:
                        continue

                    for pattern in self._patterns:
                        if timeframe not in pattern.timeframes:
                            continue
                        signal = pattern.analyze(snapshot, self._store)
                        if signal:
                            self._record_detection(signal)
                            await self._process_signal(signal, pattern)

        self._save_scan_charts(all_timeframes)

    # ── Signal pipeline ────────────────────────────────────────────────────────
    async def _process_signal(
        self, signal: TradeSignal, pattern: BasePattern
    ) -> None:
        log.info(
            f"Signal | {signal.symbol} {signal.timeframe} | "
            f"{signal.action} | pattern={signal.pattern} | "
            f"confidence={signal.confidence:.2f}"
        )

        # Step 1 — Vision confirmation (if enabled and confidence is sufficient)
        if (settings.vision_confirmation_enabled
                and signal.confidence >= settings.vision_min_indicator_confidence):
            verdict = await self._run_vision_check(signal, pattern)
            if verdict != VisionVerdict.CONFIRM:
                log.info(
                    f"Signal REJECTED by vision check "
                    f"({verdict}) — {signal.symbol} {signal.pattern}"
                )
                return
        elif signal.confidence < settings.vision_min_indicator_confidence:
            log.info(
                f"Signal confidence {signal.confidence:.2f} below threshold "
                f"{settings.vision_min_indicator_confidence} — skipping vision, skipping trade"
            )
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

        # Step 3 — Place the order (disabled while IBKR is commented out)
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

    def _save_scan_charts(self, timeframes: set[str]) -> None:
        """Write PNG charts for every symbol/timeframe after each scan."""
        for symbol in settings.symbols:
            for timeframe in timeframes:
                df = self._store.get_df(symbol, timeframe, min_bars=1)
                if df is None:
                    continue
                try:
                    self._renderer.render_with_ema(symbol, timeframe, df)
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
            signal.symbol, signal.timeframe, df
        )
        return self._vision.check(
            chart_png=chart_png,
            pattern_name=pattern.name,
            pattern_description=pattern.chart_description,
            symbol=signal.symbol,
            action=signal.action,
        )

    # ── Pattern discovery ──────────────────────────────────────────────────────
    def _init_patterns_detected_file(self) -> None:
        PATTERNS_DETECTED_FILE.write_text(
            "# Pattern detections (pattern_001_ema_crossover excluded)\n"
            "# timestamp | pattern | file | symbol | timeframe | action | confidence\n\n",
            encoding="utf-8",
        )

    def _record_detection(self, signal: TradeSignal) -> None:
        if signal.pattern in EXCLUDED_PATTERNS:
            return
        filename = self._pattern_files.get(signal.pattern, "?")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (
            f"{ts} | {signal.pattern} | {filename} | {signal.symbol} | "
            f"{signal.timeframe} | {signal.action} | "
            f"confidence={signal.confidence:.2f}\n"
        )
        with PATTERNS_DETECTED_FILE.open("a", encoding="utf-8") as f:
            f.write(line)

    def _discover_patterns(self) -> None:
        for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
            if module_info.name.startswith("pattern_"):
                module = importlib.import_module(f"patterns.{module_info.name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type)
                            and issubclass(attr, BasePattern)
                            and attr is not BasePattern):
                        instance = attr()
                        self._patterns.append(instance)
                        self._pattern_files[instance.name] = f"patterns/{module_info.name}.py"
                        log.info(f"Scanner | Registered pattern: {instance}")
