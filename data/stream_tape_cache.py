"""On-disk cache of paper-stream tapes.

A Start used to open a new stream process and GET /api/history once per
symbol. Tapes are reused for STREAM_TAPE_CACHE_TTL_S (same after_ts/limit)
so the next Start reads local JSON instead of repeating those round trips.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STREAM_TAPE_CACHE_TTL_S = 18 * 3600
CACHE_MISS = object()


def cache_root() -> Path:
    override = (os.environ.get("STREAM_TAPE_CACHE_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "barcache" / "stream_tapes"


def _path(market: str | None, symbol: str, after_ts: int | None, limit: int) -> Path:
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in symbol.upper()
    )
    mid = (market or "us").lower()
    after = "none" if after_ts is None else str(int(after_ts))
    return cache_root() / mid / f"{safe}_{after}_{int(limit)}.json"


def cache_get(
    market: str | None,
    symbol: str,
    after_ts: int | None,
    limit: int,
) -> list | object:
    """Tape rows, or CACHE_MISS when absent, stale, or unreadable."""
    path = _path(market, symbol, after_ts, limit)
    if not path.is_file():
        return CACHE_MISS
    try:
        payload = json.loads(path.read_text())
        saved = float(payload["saved_at"])
        rows = payload["rows"]
    except (OSError, ValueError, KeyError, TypeError):
        return CACHE_MISS
    if time.time() - saved > STREAM_TAPE_CACHE_TTL_S:
        return CACHE_MISS
    if not isinstance(rows, list):
        return CACHE_MISS
    return rows


def cache_put(
    market: str | None,
    symbol: str,
    after_ts: int | None,
    limit: int,
    rows: list,
) -> None:
    path = _path(market, symbol, after_ts, limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"saved_at": time.time(), "rows": rows},
        separators=(",", ":"),
    ))
    tmp.replace(path)
