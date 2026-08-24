"""Shared business logic for the web UI (mirrors tk ui/app.py flows)."""

from __future__ import annotations

import base64
import importlib
import io
import pkgutil
from typing import Any, Optional

import patterns as patterns_pkg
from analysis.chart_renderer import ChartRenderer
from analysis.price_volume import volume_confirm_gate
from config import settings, DISABLED_PATTERNS
from core.engine_defaults import passes_min_confidence, passes_regime_filter
from core.kronos_gate import kronos_gate_check
from core.market import get_market
from data.ohlcv_store import OHLCVStore, DEFAULT_WINDOW
from data.tv_client import TVClient
from data.history import fetch_ohlcv_candles
from patterns.base_pattern import BasePattern, TradeSignal, skip_pattern_module
from patterns.chart_scan import latest_signals_over_lookback
from utils.logger import log

TIMEFRAMES = ["1d", "1W"]


def discover_patterns(
    disabled_patterns: list[str] | None = None,
) -> list[BasePattern]:
    """Instantiate patterns for the explorer — same skip rules as MarketScanner.

    Skips ``instance.skipped`` and names in ``disabled_patterns`` (defaults to
    ``DISABLED_PATTERNS`` from config).
    """
    disabled = set(disabled_patterns if disabled_patterns is not None else DISABLED_PATTERNS)
    found: list[BasePattern] = []
    for module_info in pkgutil.iter_modules(patterns_pkg.__path__):
        if skip_pattern_module(module_info.name):
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
                    if instance.skipped or instance.name in disabled:
                        continue
                    found.append(instance)
                except Exception as exc:
                    log.warning(f"Web | Failed to instantiate {attr_name}: {exc}")
    return found


class ExplorerService:
    def __init__(self) -> None:
        profile = get_market()
        self._bind_market(profile)
        self._patterns = discover_patterns()

    def _bind_market(self, profile) -> None:
        self._market = profile.id
        self._tv = TVClient(
            profile.tv_screener,
            profile.tv_exchange,
            exchange_overrides=None,
        )
        self._renderer = ChartRenderer(save_to_disk=False, session_tz=profile.session_tz)
        self._store = OHLCVStore(
            window=max(DEFAULT_WINDOW, settings.tv_history_days),
            session_tz=profile.session_tz,
        )

    def fetch_symbols(self, n: int, market: str | None = None) -> list[dict[str, str]]:
        profile = get_market(market)
        if profile.id != getattr(self, "_market", None):
            self._bind_market(profile)
        rows = TVClient.fetch_universe_cached(n, profile.id)
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
        market: str | None = None,
    ) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if market:
            profile = get_market(market)
        elif exchange.upper() == "PSE":
            profile = get_market("ph")
        else:
            profile = get_market("us")
        if profile.id != getattr(self, "_market", None):
            self._bind_market(profile)

        candles = fetch_ohlcv_candles(
            symbol, timeframe, exchange=exchange, tv_client=self._tv,
        )
        if not candles:
            raise ValueError(f"No history available for {symbol} {timeframe}.")

        self._store.replace_all(symbol, timeframe, candles)
        df = self._store.get_df(symbol, timeframe, min_bars=2)
        if df is None:
            raise ValueError(f"Insufficient bars for {symbol} {timeframe}.")

        use_kronos = settings.kronos_gate_enabled if kronos_gate is None else kronos_gate
        use_volume = settings.volume_gate_enabled if volume_gate is None else volume_gate

        signals: list[TradeSignal] = []
        if run_patterns:
            raw = latest_signals_over_lookback(
                self._patterns, symbol, timeframe, candles,
                session_tz=profile.session_tz,
            )
            for sig in raw:
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
