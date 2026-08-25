"""Unit checks for weekday --update-db crontab merge (no live crontab)."""

from pathlib import Path

from data.update_cron import (
    MARKER_BEGIN,
    MARKER_END,
    managed_block,
    merge_crontab,
)


def test_merge_installs_into_empty_crontab():
    block = managed_block(Path("/home/deploy/apps/trading"), "/venv/bin/python")
    new, changed = merge_crontab("", block)
    assert changed
    assert MARKER_BEGIN in new
    assert MARKER_END in new
    assert "CRON_TZ=America/New_York" in new
    assert "30 16 * * 1-5" in new
    assert "main.py --update-db" in new


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
