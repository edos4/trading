"""Persist the uploaded paper-replay export so it survives server restarts.

The store holds a single replay slot: the raw JSON the operator uploaded from
the Export Trades button (or --export-trades-log), keyed by nothing but written
verbatim to ``data/cache/replay.json``. The Replay tab transforms it client-side
into the same shape the Paper tab renders.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from utils.logger import log

REPO_ROOT = Path(__file__).resolve().parent.parent
REPLAY_PATH = REPO_ROOT / "data" / "cache" / "replay.json"

_lock = threading.Lock()


def load() -> dict[str, Any] | None:
    """Return the persisted replay payload, or None when there is no replay."""
    if not REPLAY_PATH.exists():
        return None
    try:
        with _lock:
            return json.loads(REPLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Replay | failed to read %s", REPLAY_PATH)
        return None


def save(payload: dict[str, Any]) -> None:
    """Overwrite the persisted replay payload."""
    REPLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPLAY_PATH.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(REPLAY_PATH)


def clear() -> None:
    """Remove the persisted replay payload."""
    try:
        with _lock:
            if REPLAY_PATH.exists():
                REPLAY_PATH.unlink()
    except OSError:
        log.exception("Replay | failed to remove %s", REPLAY_PATH)
