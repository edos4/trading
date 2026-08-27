"""Unit checks for data.update last-session targeting."""

from datetime import datetime
from zoneinfo import ZoneInfo

from data.update import _last_trading_date

_NY = ZoneInfo("America/New_York")


def test_monday_morning_targets_friday():
    now = datetime(2026, 8, 24, 6, 42, tzinfo=_NY)
    assert _last_trading_date(now).isoformat() == "2026-08-21"


def test_friday_afternoon_before_close_targets_thursday():
    now = datetime(2026, 8, 21, 15, 59, tzinfo=_NY)
    assert _last_trading_date(now).isoformat() == "2026-08-20"


def test_friday_after_close_targets_friday():
    now = datetime(2026, 8, 21, 16, 0, tzinfo=_NY)
    assert _last_trading_date(now).isoformat() == "2026-08-21"


def test_saturday_targets_friday():
    now = datetime(2026, 8, 22, 12, 0, tzinfo=_NY)
    assert _last_trading_date(now).isoformat() == "2026-08-21"


def test_ph_after_close_targets_that_day():
    manila = ZoneInfo("Asia/Manila")
    now = datetime(2026, 8, 26, 15, 30, tzinfo=manila)
    assert _last_trading_date(now, "ph").isoformat() == "2026-08-26"


def test_ph_before_close_targets_prior_session():
    manila = ZoneInfo("Asia/Manila")
    now = datetime(2026, 8, 26, 10, 0, tzinfo=manila)
    assert _last_trading_date(now, "ph").isoformat() == "2026-08-25"
