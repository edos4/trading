"""
patterns/_dedup.py — per-symbol consumed-pivot registry.

The `.cjs` pattern scans keep a `usedSH1` / `usedSH2` set and skip any anchor
already consumed by an earlier trade, so one swing pivot yields at most one
trade. In the Python walk-forward engine the pattern's ``analyze()`` is
stateless and called once per bar, so the "already used" check has to live
somewhere both the engine and the pattern scan can see.

``core.backtester._core_backtest_symbol`` calls ``reset()`` at the start of each
symbol and ``mark(*pivots)`` whenever a signal is consumed (trade / block /
filter). Pattern detectors call ``used(i)`` / ``any_used(iterable)`` inside
their scan loops. Outside a backtest (paper / scanner / Explorer) the set stays
empty and every check is a no-op.
"""

from __future__ import annotations

_used: set[int] = set()
_gen: int = 0
# The backtest walk index. During a backtest the store is seeded with a small
# look-ahead window (so pattern scans match the `.cjs` full-history scans), so
# `len(df) - 1` is NOT the current bar — patterns must use `current_bar()`.
# None outside a backtest (paper / scanner / Explorer): fall back to len(df)-1.
_current: int | None = None


def reset() -> None:
    _used.clear()
    global _current, _gen
    _current = None
    _gen += 1


def generation() -> int:
    """Bumped by reset() — detectors key per-symbol caches on this."""
    return _gen


def set_current(bar: int | None) -> None:
    global _current
    _current = bar


def current_bar(default: int) -> int:
    return default if _current is None else _current


def mark(*pivots: int) -> None:
    _used.update(int(p) for p in pivots if p is not None)


def used(pivot: int) -> bool:
    return pivot in _used


def any_used(pivots) -> bool:
    return any(p in _used for p in pivots)
