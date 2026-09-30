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

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

from config import PATTERN_SCAN_HISTORY_BARS, settings
from data.ohlcv_store import DEFAULT_WINDOW, OHLCVStore
from data.tv_client import MarketSnapshot, OHLCVCandle
from patterns.base_pattern import BasePattern, TradeSignal
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


def load_patterns(disabled, version_set=None):
    from core.pattern_loader import discover
    return discover(disabled, version_set)


def init_analyze_worker(
    disabled: list[str],
    session_tz: str,
    skip_edgar: bool,
    window: int = DEFAULT_WINDOW,
    version_set: dict | None = None,
    store_config: dict | None = None,
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
    if version_set is None:
        raise ValueError('Analyze workers require a pinned version set')
    from core.pattern_loader import discover
    from core.pattern_edit_store import open_pattern_store
    _worker_patterns = discover(
        disabled, version_set, store=open_pattern_store(**(store_config or {})),
        in_process=True,
    )
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
    version_set: dict,
    store=None,
) -> ProcessPoolExecutor | None:
    if workers <= 1:
        return None
    ctx = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=init_analyze_worker,
        initargs=(list(disabled), session_tz, bool(skip_edgar), int(window), dict(version_set),
                  None if store is None else dict(root=str(store.root), dsn=store._dsn, schema=store.schema)),
    )


def _min_bars(pattern: BasePattern) -> int:
    return max(int(getattr(pattern, "MIN_BARS", 2) or 2), PATTERN_SCAN_HISTORY_BARS)


def analyze_batch(
    jobs: list[tuple[MarketSnapshot, list[OHLCVCandle]]],
) -> list[tuple[int, list[TradeSignal]]]:
    """Picklable process-pool entry: one private-store analyze per snapshot.

    Patterns are versioned (``VersionPattern``, which implements
    ``analyze_many``). Each pattern walks the whole batch before the next one
    starts, so a version that still needs the sandbox pays one worker start per
    chunk instead of one per symbol; one preloaded here simply runs in-process.
    """
    store = _worker_store
    if store is None:
        raise RuntimeError("analyze worker not initialized")
    from data.edgar_client import set_skip_edgar

    set_skip_edgar(_worker_skip_edgar)
    prepared: list[tuple[MarketSnapshot, int]] = []
    for snapshot, candles in jobs:
        if candles:
            store.replace_all(snapshot.symbol, snapshot.timeframe, candles)
        prepared.append((snapshot, store.available(snapshot.symbol, snapshot.timeframe)))

    evaluations = [0] * len(jobs)
    hits: list[list[TradeSignal]] = [[] for _ in jobs]
    for pattern in _worker_patterns:
        targets = [
            (index, snapshot)
            for index, (snapshot, n_bars) in enumerate(prepared)
            if snapshot.timeframe in pattern.timeframes and n_bars >= _min_bars(pattern)
        ]
        if not targets:
            continue
        for index, _snapshot in targets:
            evaluations[index] += 1
        try:
            signals = pattern.analyze_many([snapshot for _index, snapshot in targets], store)
        except Exception:
            log.exception(f"analyze | {pattern.name} batch failed")
            continue
        for (index, _snapshot), signal in zip(targets, signals):
            if signal:
                hits[index].append(signal)
    return [(evaluations[index], hits[index]) for index in range(len(jobs))]
