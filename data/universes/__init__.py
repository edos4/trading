"""
data/universes/ — named ticker lists for offline pattern backtests.

Each `<name>.txt` is whitespace-separated tickers (one per line or many;
blank lines and `#` comments ignored).
Lists were copied once, by hand, from the locked `.cjs` pattern-backtest scripts
at C:\\Users\\dell\\tradingview-mcp so the Python engine sweeps the same universe
the documented numbers were computed on. They are NOT read from that repo at
runtime — this package is the source of truth.

    from data.universes import load
    symbols = load("upward_channel")        # list[str], de-duped, order preserved
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def available() -> list[str]:
    """Names that `load()` accepts."""
    return sorted(p.stem for p in _DIR.glob("*.txt"))


def load(name: str) -> list[str]:
    """Return the ticker list for `name` (e.g. 'flag', 'default').

    Raises FileNotFoundError with the available names if `name` is unknown.
    """
    path = _DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"unknown universe {name!r}; available: {', '.join(available())}"
        )
    seen: set[str] = set()
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        for token in raw.split("#", 1)[0].split():
            ticker = token.strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            out.append(ticker)
    return out
