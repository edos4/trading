"""Install the weekday stocks_history `--update-db` crontab (after US close).

Ubuntu vixie cron (3.0pl1) ignores `CRON_TZ`, so a `30 16 * * 1-5` line on a
Europe/Berlin VPS fires at 16:30 CEST (10:30 ET), before the cash close.

Instead: poll every 15 minutes weekdays, then `is_due()` gates on America/New_York
(16:30+, Mon–Fri) and the last-update stamp. Idempotent marked block; flock so a
long Yahoo pass cannot overlap the next tick.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.logger import log

MARKER_BEGIN = "# --- stocks_history-update-db BEGIN ---"
MARKER_END = "# --- stocks_history-update-db END ---"
_NY_TZ = ZoneInfo("America/New_York")
# Wall-clock is NY inside is_due(); */15 is server-local and DST-safe.
CRON_SCHEDULE = "*/15 * * * 1-5"
_UNSET = object()


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_due(
    now: datetime | None = None,
    stamp: date | None | object = _UNSET,
) -> bool:
    """True after 16:30 ET on a weekday when the stamp is behind last close."""
    from data.update import _last_trading_date

    now = now or datetime.now(tz=_NY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_NY_TZ)
    else:
        now = now.astimezone(_NY_TZ)
    if now.weekday() >= 5:
        return False
    if now.hour < 16 or (now.hour == 16 and now.minute < 30):
        return False
    if stamp is _UNSET:
        from data.history_stamp import read_stamp

        stamp = read_stamp()
    last = _last_trading_date(now)
    if stamp is not None and stamp >= last:
        return False
    return True


def managed_block(app_dir: Path, python: str) -> str:
    logs = app_dir / "logs"
    log_path = logs / "db_update.log"
    lock_path = logs / "update-db.lock"
    inner = (
        f"mkdir -p {logs} && cd {app_dir} && "
        f"{python} -m data.update_cron >> {log_path} 2>&1"
    )
    job = f"{CRON_SCHEDULE} flock -n -E 0 {lock_path} sh -c '{inner}'"
    return (
        f"{MARKER_BEGIN}\n"
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
    """Create or replace the weekday --update-db cron. True if crontab changed."""
    app_dir = (app_dir or _app_root()).resolve()
    python = python or sys.executable
    block = managed_block(app_dir, python)
    new, changed = merge_crontab(_crontab_list(), block)
    if not changed:
        log.info(
            "stocks_history | weekday --update-db cron already installed "
            "(every 15m Mon-Fri; runs after 16:30 America/New_York)"
        )
        return False
    _crontab_set(new)
    log.info(
        "stocks_history | installed weekday --update-db cron "
        f"(every 15m Mon-Fri; runs after 16:30 America/New_York) in {app_dir}"
    )
    return True


def main() -> int:
    if not is_due():
        return 0
    from data.update import run_update

    log.info("stocks_history | --update-db due after US cash close")
    run_update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
