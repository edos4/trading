"""Install the weekday stocks_history `--update-db` crontab (after US close).

Schedule is 16:30 America/New_York, Monday–Friday, so Yahoo has the cash-session
OHLCV close. Idempotent: a marked block is inserted or replaced, never duplicated.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from utils.logger import log

MARKER_BEGIN = "# --- stocks_history-update-db BEGIN ---"
MARKER_END = "# --- stocks_history-update-db END ---"
# 16:30 ET: US cash close is 16:00; extra 30m for the official daily bar.
CRON_SCHEDULE = "30 16 * * 1-5"


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def managed_block(app_dir: Path, python: str) -> str:
    logs = app_dir / "logs"
    log_path = logs / "db_update.log"
    job = (
        f"{CRON_SCHEDULE} mkdir -p {logs} && "
        f"cd {app_dir} && {python} main.py --update-db >> {log_path} 2>&1"
    )
    return (
        f"{MARKER_BEGIN}\n"
        f"CRON_TZ=America/New_York\n"
        f"TZ=America/New_York\n"
        f"{job}\n"
        f"{MARKER_END}\n"
    )


def merge_crontab(existing: str, block: str) -> tuple[str, bool]:
    """Return (new crontab text, whether it differs from existing)."""
    existing = (existing or "").replace("\r\n", "\n")
    first = existing.strip().splitlines()[0] if existing.strip() else ""
    if first.lower().startswith("no crontab"):
        existing = ""
    if MARKER_BEGIN in existing and MARKER_END in existing:
        start = existing.index(MARKER_BEGIN)
        end = existing.index(MARKER_END) + len(MARKER_END)
        if end < len(existing) and existing[end] == "\n":
            end += 1
        new = existing[:start] + block + existing[end:]
    else:
        prefix = existing.rstrip()
        new = (prefix + "\n\n" if prefix else "") + block
    if not new.endswith("\n"):
        new += "\n"
    return new, new.strip() != existing.strip()


def _crontab_list() -> str:
    r = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def _crontab_set(text: str) -> None:
    subprocess.run(
        ["crontab", "-"],
        input=text,
        text=True,
        check=True,
        capture_output=True,
    )


def ensure_weekday_update_cron(
    *,
    app_dir: Path | None = None,
    python: str | None = None,
) -> bool:
    """Create the weekday --update-db cron if missing. True if crontab changed."""
    app_dir = (app_dir or _app_root()).resolve()
    python = python or sys.executable
    block = managed_block(app_dir, python)
    new, changed = merge_crontab(_crontab_list(), block)
    if not changed:
        log.info(
            "stocks_history | weekday --update-db cron already installed "
            "(16:30 America/New_York, Mon-Fri)"
        )
        return False
    _crontab_set(new)
    log.info(
        "stocks_history | installed weekday --update-db cron "
        f"(16:30 America/New_York Mon-Fri) in {app_dir}"
    )
    return True
