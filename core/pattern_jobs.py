"""Off-loop pattern.analyze() — spawn workers, private candle copies.

Ledger writes, pending fills, Kronos/vision, and PaperAccount stay on the
scanner event loop. Workers only run BasePattern.analyze() against a
throwaway OHLCVStore built from a copied candle list.

Spawn (not fork): the scanner lives in a PaperBook thread with a running
asyncio loop; fork would snapshot that state into children. Spawn
re-imports __main__ as __mp_main__ (so main.py's `if __name__ == "__main__"`
guard must stay — it already does).
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import pkgutil
from concurrent.futures import ProcessPoolExecutor

import patterns as patterns_pkg
from config import PATTERN_SCAN_HISTORY_BARS, settings
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, skip_pattern_module, TradeSignal
from utils.logger import log

_worker_patterns: list[BasePattern] = []
_worker_store: OHLCVStore | None = None
_worker_skip_edgar = False


def analyze_worker_count(configured: int | None = None) -> int:
    """1 = inline on the scan loop. 0 = auto 2–8 from cpu_count. Else clamp 2–32."""
    n = settings.scanner_analyze_workers if configured is None else int(configured)
    if n == 1:
        return 1
    if n > 1:
        return max(2, min(32, n))
    cpu = os.cpu_count() or 4
    return max(2, min(8, cpu))


def load_patterns(disabled: set[str] | list[str]) -> list[BasePattern]:
    """Same discovery as MarketScanner, without scanner-side logging."""
    blocked = set(disabled)
    found: list[BasePattern] = []
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
                if instance.skipped or instance.name in blocked:
                    continue
                found.append(instance)
    return found


def init_analyze_worker(
    disabled: list[str],
    session_tz: str,
    skip_edgar: bool,
    window: int = DEFAULT_WINDOW,
) -> None:
    """ProcessPoolExecutor initializer — runs once per spawned worker."""
    global _worker_patterns, _worker_store, _worker_skip_edgar
    from data.edgar_client import set_skip_edgar

    _worker_skip_edgar = bool(skip_edgar)
    set_skip_edgar(_worker_skip_edgar)
    _worker_store = OHLCVStore(
        window=max(int(window), DEFAULT_WINDOW),
        session_tz=session_tz or "America/New_York",
    )
    _worker_patterns = load_patterns(disabled)
    log.debug(
        f"analyze worker pid={os.getpid()} patterns={len(_worker_patterns)}"
    )


def make_analyze_pool(
    *,
    disabled: list[str] | set[str],
    session_tz: str,
    skip_edgar: bool,
    window: int,
    workers: int,
) -> ProcessPoolExecutor | None:
    if workers <= 1:
        return None
    ctx = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=init_analyze_worker,
        initargs=(list(disabled), session_tz, bool(skip_edgar), int(window)),
    )


def _min_bars(pattern: BasePattern) -> int:
    return max(int(getattr(pattern, "MIN_BARS", 2) or 2), PATTERN_SCAN_HISTORY_BARS)


def _analyze_one(
    snapshot: MarketSnapshot, candles: list[OHLCVCandle],
) -> tuple[int, list[TradeSignal]]:
    store = _worker_store
    if store is None:
        raise RuntimeError("analyze worker not initialized")
    from data.edgar_client import set_skip_edgar

    set_skip_edgar(_worker_skip_edgar)
    if candles:
        store.replace_all(snapshot.symbol, snapshot.timeframe, candles)
    n_bars = store.available(snapshot.symbol, snapshot.timeframe)
    n_eval = 0
    hits: list[TradeSignal] = []
    for pattern in _worker_patterns:
        if snapshot.timeframe not in pattern.timeframes:
            continue
        if n_bars < _min_bars(pattern):
            continue
        n_eval += 1
        try:
            signal = pattern.analyze(snapshot, store)
        except Exception:
            log.exception(
                f"analyze | {pattern.name} {snapshot.symbol} {snapshot.timeframe}"
            )
            continue
        if signal:
            hits.append(signal)
    return n_eval, hits


def analyze_batch(
    jobs: list[tuple[MarketSnapshot, list[OHLCVCandle]]],
) -> list[tuple[int, list[TradeSignal]]]:
    """Picklable process-pool entry: one private-store analyze per snapshot."""
    out: list[tuple[int, list[TradeSignal]]] = []
    for snapshot, candles in jobs:
        try:
            out.append(_analyze_one(snapshot, candles))
        except Exception:
            log.exception(
                f"analyze | batch job failed {getattr(snapshot, 'symbol', '?')}"
            )
            out.append((0, []))
    return out
