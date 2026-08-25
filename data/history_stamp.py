"""Stamp file for the last US cash session ingested into stocks_history.

`logs/stocks_history_updated.txt` holds an ISO date (America/New_York session).
Web start reads it when present; `--update-db` rewrites it after a run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STAMP_NAME = "stocks_history_updated.txt"


def stamp_path() -> Path:
    return _REPO_ROOT / "logs" / _STAMP_NAME


def read_stamp(path: Path | None = None) -> date | None:
    """Return the stamped session date, or None if the file is missing/invalid."""
    path = path or stamp_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            return date.fromisoformat(line.split()[0])
        except ValueError:
            continue
    return None


def write_stamp(day: date, path: Path | None = None) -> Path:
    """Atomically write `YYYY-MM-DD` as the last ingested US session."""
    path = path or stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{day.isoformat()}\n", encoding="utf-8")
    tmp.replace(path)
    return path


def plan_web_freshness(
    stamp: date | None,
    db_median: date | None,
    last_trading: date,
) -> str:
    """Decide what web start should do.

    - file exists and is current → `noop` (just read it)
    - file exists but behind last US close → `update`
    - no file, DB already on last US close → `write_stamp`
    - no file, DB missing/stale → `update` (then write the stamp)
    """
    if stamp is not None:
        return "noop" if stamp >= last_trading else "update"
    if db_median is not None and db_median >= last_trading:
        return "write_stamp"
    return "update"
