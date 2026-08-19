"""Persistent paper signal log (JSONL) for the web Paper Logs tab.

One file per market: logs/paper_signals_{us,ph}.jsonl
The in-memory scanner deque is a cache; this file is what the UI reads
and what Reset clears.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.market import get_market

SIGNAL_LOG_MAX = 1000

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Tests point this at a temp dir so we never touch the real logs/.
_log_dir: Path | None = None
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def log_dir() -> Path:
    return _log_dir if _log_dir is not None else _REPO_ROOT / "logs"


def signal_log_path(market: str) -> Path:
    return log_dir() / f"paper_signals_{get_market(market).id}.jsonl"


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def load_signal_log(market: str, *, limit: int = SIGNAL_LOG_MAX) -> list[dict[str, Any]]:
    path = signal_log_path(market)
    with _lock_for(path):
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return rows


def append_signal_log(market: str, entry: dict[str, Any]) -> None:
    path = signal_log_path(market)
    payload = dict(entry)
    payload.setdefault("market", get_market(market).id)
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _compact_unlocked(path)


def reset_signal_log(market: str) -> None:
    """Wipe the log. Truncate in place so the path stays stable for tail/editors."""
    path = signal_log_path(market)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def _compact_unlocked(path: Path, *, limit: int = SIGNAL_LOG_MAX) -> None:
    """Rewrite the file to the last `limit` rows when it grows past 2× limit."""
    try:
        n_lines = 0
        with path.open("r", encoding="utf-8") as fh:
            for _ in fh:
                n_lines += 1
    except OSError:
        return
    if n_lines <= limit * 2:
        return
    rows: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(line if line.endswith("\n") else line + "\n")
    keep = rows[-limit:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(keep)
    tmp.replace(path)
