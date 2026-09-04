"""
data/barcache.py — frozen offline daily-bar snapshots for pattern backtests.

One JSON file per symbol under ``data/barcache/<market>/<SYMBOL>.json``:

    {"symbol": "NVDA", "timeframe": "1d", "fetched_utc": "2026-09-04T12:00:00+00:00",
     "source": "fetch_ohlcv_candles",
     "bars": [{"t": 1690848000, "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 1234}, ...]}

``t`` is a unix-**seconds** UTC session timestamp — the same shape the ``.cjs``
pattern-backtest scripts use, so ported detection maths line up bar-for-bar.

This store is deliberately separate from the live scanner's 6h-TTL
``data/cache/{key}.json``: it has **no TTL**. It is a research snapshot you
rebuild explicitly with ``scripts/build_barcache.py`` (live fetch) or
``scripts/import_cjs_barcache.py`` (one-time convert from a .cjs cache dir).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from data.tv_client import OHLCVCandle

BARCACHE_ROOT = Path("data/barcache")


def market_dir(market: str, root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else BARCACHE_ROOT
    return base / (market or "us")


def cache_path(market: str, symbol: str, root: Path | str | None = None) -> Path:
    return market_dir(market, root) / f"{symbol.upper()}.json"


def write(
    market: str,
    symbol: str,
    candles: list[OHLCVCandle],
    *,
    source: str = "unknown",
    root: Path | str | None = None,
) -> Path:
    """Serialise `candles` to the barcache. Returns the file path written."""
    path = cache_path(market, symbol, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    bars = []
    for c in candles:
        ts = c.timestamp
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bars.append({
            "t": int(ts.timestamp()),
            "o": float(c.open),
            "h": float(c.high),
            "l": float(c.low),
            "c": float(c.close),
            "v": float(c.volume or 0),
        })
    payload = {
        "symbol": symbol.upper(),
        "timeframe": "1d",
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "bars": bars,
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path


def load(
    market: str, symbol: str, root: Path | str | None = None
) -> list[OHLCVCandle] | None:
    """Return stored candles (oldest first) or None if the symbol isn't cached."""
    path = cache_path(market, symbol, root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out: list[OHLCVCandle] = []
    for b in payload.get("bars", []):
        out.append(OHLCVCandle(
            open=float(b["o"]),
            high=float(b["h"]),
            low=float(b["l"]),
            close=float(b["c"]),
            volume=float(b.get("v") or 0),
            timestamp=datetime.fromtimestamp(int(b["t"]), tz=timezone.utc),
        ))
    return out


def available(market: str, root: Path | str | None = None) -> list[str]:
    """Sorted list of symbols currently cached for `market`."""
    d = market_dir(market, root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if not p.stem.startswith("_"))


def load_earnings_cache(root: Path | str | None = None) -> dict[str, list[int]]:
    """{"NVDA": [unix-sec 8-K/2.02 filing dates]} — empty dict if absent."""
    base = Path(root) if root is not None else BARCACHE_ROOT
    path = base / "earnings_cache.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k.upper(): [int(x) for x in v] for k, v in raw.items()}
