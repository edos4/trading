"""Unit checks for the stocks_history last-update stamp file."""

from datetime import date
from pathlib import Path

from data.history_stamp import plan_web_freshness, read_stamp, write_stamp


def test_read_stamp_missing(tmp_path: Path):
    assert read_stamp(tmp_path / "missing.txt") is None


def test_write_and_read_stamp(tmp_path: Path):
    path = tmp_path / "logs" / "stocks_history_updated.txt"
    write_stamp(date(2026, 8, 25), path)
    assert read_stamp(path) == date(2026, 8, 25)
    assert path.read_text(encoding="utf-8") == "2026-08-25\n"


def test_read_stamp_skips_comments(tmp_path: Path):
    path = tmp_path / "stamp.txt"
    path.write_text("# last US session\n2026-08-21\n", encoding="utf-8")
    assert read_stamp(path) == date(2026, 8, 21)


def test_read_stamp_invalid(tmp_path: Path):
    path = tmp_path / "stamp.txt"
    path.write_text("not-a-date\n", encoding="utf-8")
    assert read_stamp(path) is None


def test_plan_file_current_is_noop():
    assert plan_web_freshness(
        date(2026, 8, 25), date(2026, 8, 21), date(2026, 8, 25)
    ) == "noop"


def test_plan_file_stale_updates():
    assert plan_web_freshness(
        date(2026, 8, 21), date(2026, 8, 21), date(2026, 8, 25)
    ) == "update"


def test_plan_no_file_db_current_writes_stamp():
    assert plan_web_freshness(
        None, date(2026, 8, 25), date(2026, 8, 25)
    ) == "write_stamp"


def test_plan_no_file_db_stale_updates():
    assert plan_web_freshness(
        None, date(2026, 8, 21), date(2026, 8, 25)
    ) == "update"


def test_plan_no_file_empty_db_updates():
    assert plan_web_freshness(None, None, date(2026, 8, 25)) == "update"
