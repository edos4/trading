"""Shared business logic for the web UI (mirrors tk ui/app.py flows)."""

from __future__ import annotations

import base64
import importlib
import io
import pkgutil
from datetime import datetime, timezone
from typing import Any, Optional

import patterns as patterns_pkg
from analysis.chart_renderer import ChartRenderer
from analysis.price_volume import volume_confirm_gate
from config import settings
from core.engine_defaults import passes_min_confidence, passes_regime_filter
from core.kronos_gate import kronos_gate_check
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.tv_client import MarketSnapshot, TVClient
from patterns.base_pattern import BasePattern, TradeSignal
from utils.logger import log

TIMEFRAMES = ["1d", "1W"]


def discover_patterns() -> list[BasePattern]:
    found: list[BasePattern] = []
    for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
        if module_info.name.startswith("_") or module_info.name == "base_pattern":
            continue
        try:
            module = importlib.import_module(f"patterns.{module_info.name}")
        except Exception as exc:
            log.warning(f"Web | Failed to import pattern {module_info.name}: {exc}")
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BasePattern)
                and attr is not BasePattern
            ):
                try:
                    instance = attr()
                    if not instance.skipped:
                        found.append(instance)
                except Exception as exc:
                    log.warning(f"Web | Failed to instantiate {attr_name}: {exc}")
    return found


class ExplorerService:
    def __init__(self) -> None:
        self._tv = TVClient(
            settings.tv_screener,
            settings.tv_exchange,
            exchange_overrides=None,
        )
        self._renderer = ChartRenderer(save_to_disk=False)
        self._patterns = discover_patterns()
        self._store = OHLCVStore(window=max(DEFAULT_WINDOW, settings.tv_history_days))

    def fetch_symbols(self, n: int) -> list[dict[str, str]]:
        rows = TVClient.fetch_top_symbols_with_exchanges_cached(n, settings.tv_screener)
        return [{"symbol": s, "exchange": ex} for s, ex in rows]

    def load_symbol(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        *,
        run_patterns: bool = True,
        kronos_gate: bool | None = None,
        volume_gate: bool | None = None,
    ) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        candles = self._tv._fetch_history_screener(symbol, exchange, timeframe)
        if not candles:
            raise ValueError(f"No history available for {symbol} {timeframe}.")

        self._store.replace_all(symbol, timeframe, candles)
        df = self._store.get_df(symbol, timeframe, min_bars=2)
        if df is None:
            raise ValueError(f"Insufficient bars for {symbol} {timeframe}.")

        latest = candles[-1]
        snapshot = MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=datetime.now(timezone.utc),
            candle=latest,
            indicators={},
            summary={"RECOMMENDATION": "NEUTRAL"},
            oscillators={},
            moving_avgs={},
        )

        use_kronos = settings.kronos_gate_enabled if kronos_gate is None else kronos_gate
        use_volume = settings.volume_gate_enabled if volume_gate is None else volume_gate

        signals: list[TradeSignal] = []
        if run_patterns:
            for pattern in self._patterns:
                if timeframe not in pattern.timeframes:
                    continue
                try:
                    sig = pattern.analyze(snapshot, self._store)
                except Exception as exc:
                    log.warning(f"Web | {pattern.name} failed on {symbol} {timeframe}: {exc}")
                    continue
                if sig is None:
                    continue
                if not passes_min_confidence(sig):
                    continue
                if not passes_regime_filter(sig, self._store):
                    continue
                if use_kronos:
                    gate = kronos_gate_check(sig, self._store)
                    if not gate.passed:
                        continue
                if use_volume:
                    vgate = volume_confirm_gate(sig, self._store)
                    if not vgate.passed:
                        continue
                signals.append(sig)

        annotations: list[dict] = []
        for s in signals:
            annotations.extend(s.chart_annotations)

        png = self._renderer.render_with_ema(
            symbol, timeframe, df, annotations=annotations or None,
        )
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        change = float(last["close"] - prev["close"])
        pct = (change / float(prev["close"]) * 100) if prev["close"] else 0.0

        csv_buf = io.StringIO()
        df.to_csv(csv_buf)

        return {
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "bars": len(df),
            "ohlc": {
                "open": float(last["open"]),
                "high": float(last["high"]),
                "low": float(last["low"]),
                "close": float(last["close"]),
                "change": change,
                "change_pct": pct,
            },
            "signals": [
                {
                    "pattern": s.pattern,
                    "action": s.action,
                    "timeframe": s.timeframe,
                    "confidence": float(s.confidence),
                    "price": float(s.price),
                    "notes": s.notes or "",
                }
                for s in signals
            ],
            "chart_png_b64": base64.b64encode(png).decode("ascii"),
            "csv": csv_buf.getvalue(),
        }


_explorer: Optional[ExplorerService] = None


def get_explorer() -> ExplorerService:
    global _explorer
    if _explorer is None:
        _explorer = ExplorerService()
    return _explorer
