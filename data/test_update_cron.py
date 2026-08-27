"""Unit checks for weekday --update-db crontab merge (no live crontab)."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data.update_cron import (
    MARKER_BEGIN,
    MARKER_END,
    is_due,
    managed_block,
    merge_crontab,
)

_NY = ZoneInfo("America/New_York")


def test_merge_installs_into_empty_crontab():
    block = managed_block(Path("/home/deploy/apps/trading"), "/venv/bin/python")
    new, changed = merge_crontab("", block)
    assert changed
    assert MARKER_BEGIN in new
    assert MARKER_END in new
    assert "CRON_TZ=" not in new
    assert "*/15 * * * 1-5" in new
    assert "flock -n -E 0" in new
    assert "python -m data.update_cron" in new
    assert "30 16 * * 1-5" not in new


def test_merge_is_idempotent():
    block = managed_block(Path("/app"), "/venv/bin/python")
    first, _ = merge_crontab("", block)
    second, changed = merge_crontab(first, block)
    assert changed is False
    assert second.strip() == first.strip()


def test_merge_preserves_other_jobs():
    existing = "0 8 * * * /usr/bin/echo hello\n"
    block = managed_block(Path("/app"), "/venv/bin/python")
    new, changed = merge_crontab(existing, block)
    assert changed
    assert "0 8 * * * /usr/bin/echo hello" in new
    assert MARKER_BEGIN in new


def test_merge_replaces_existing_block():
    old = managed_block(Path("/old"), "/old/python")
    new_block = managed_block(Path("/new"), "/new/python")
    merged, changed = merge_crontab(old, new_block)
    assert changed
    assert "/new/python" in merged
    assert "/old/python" not in merged
    assert merged.count(MARKER_BEGIN) == 1


def test_merge_replaces_legacy_cron_tz_block():
    legacy = (
        f"{MARKER_BEGIN}\n"
        "CRON_TZ=America/New_York\n"
        "TZ=America/New_York\n"
        "30 16 * * 1-5 cd /app && /venv/bin/python main.py --update-db "
        ">> /app/logs/db_update.log 2>&1\n"
        f"{MARKER_END}\n"
    )
    block = managed_block(Path("/app"), "/venv/bin/python")
    merged, changed = merge_crontab(legacy, block)
    assert changed
    assert "CRON_TZ=" not in merged
    assert "30 16 * * 1-5" not in merged
    assert "python -m data.update_cron" in merged
    assert merged.count(MARKER_BEGIN) == 1


def _et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_NY)


def test_is_due_before_close_is_false():
    # Wednesday 10:30 ET — the CEST 16:30 false fire.
    assert is_due(now=_et(2026, 8, 26, 10, 30), stamp=date(2026, 8, 25)) is False
    assert is_due(now=_et(2026, 8, 26, 16, 29), stamp=date(2026, 8, 25)) is False


def test_is_due_after_close_when_stamp_behind():
    assert is_due(now=_et(2026, 8, 26, 16, 30), stamp=date(2026, 8, 25)) is True
    assert is_due(now=_et(2026, 8, 26, 22, 0), stamp=date(2026, 8, 25)) is True


def test_is_due_skips_when_stamp_current_or_weekend():
    assert is_due(now=_et(2026, 8, 26, 16, 30), stamp=date(2026, 8, 26)) is False
    assert is_due(now=_et(2026, 8, 29, 17, 0), stamp=date(2026, 8, 25)) is False
    assert is_due(now=_et(2026, 8, 26, 16, 30), stamp=None) is True
