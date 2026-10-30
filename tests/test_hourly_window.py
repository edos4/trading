"""The Alpaca hourly fetch window must stay inside two hard limits.

Alpaca's free plan answers HTTP 403 `subscription does not permit querying
recent SIP data` for any window that reaches into the last 15 minutes, and the
whole 2026-10-26 incremental run aborted on that. The same 15 minutes is also
what keeps a *forming* bar out of the table: bars are written with
`ON CONFLICT DO NOTHING`, so a half-built candle that slipped in could never be
corrected afterwards.

Both are enforced by the request `end`: `truncate(now - 15 min) - 1 s`. These
tests pin that — every bar the window admits has already closed, the window
never touches the delay, and the target session switches to "today" only once
today's first bar is readable.

Patterns read completed 1h bars, so a partial candle is a wrong input, not a
fresher one; there is deliberately no `DO UPDATE` path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from data.update_hourly import (
    _DATA_DELAY,
    _FIRST_BAR_HOUR,
    _expected_last_open,
    _session_end,
    _target_session,
)

NY = ZoneInfo("America/New_York")

# Mon 2026-10-26 is a full session; Sat 2026-10-31 is not.
MONDAY = datetime(2026, 10, 26)
TUESDAY = datetime(2026, 10, 27)
FRIDAY = datetime(2026, 10, 30)


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, tzinfo=NY)


def _sweep() -> list[datetime]:
    """Every weekday hour boundary plus the minutes around a cron tick."""
    out: list[datetime] = []
    for day in (MONDAY, TUESDAY, FRIDAY):
        for hour in range(24):
            for minute in (0, 5, 19, 20, 21, 45, 59):
                out.append(_at(day, hour, minute))
    return out


@pytest.mark.parametrize("now", _sweep())
def test_never_requests_inside_the_sip_delay(now: datetime) -> None:
    """A window that touches now-15min is what triggered the 403 abort."""
    target = _target_session(now)
    end = _session_end(target, now)
    assert end <= now - _DATA_DELAY


@pytest.mark.parametrize("now", _sweep())
def test_only_closed_bars_are_requested(now: datetime) -> None:
    """The newest bar the window admits must have closed before the delay."""
    target = _target_session(now)
    end = _session_end(target, now)
    newest = datetime.fromtimestamp(_expected_last_open(target, end), NY)
    assert newest + timedelta(hours=1) <= now - _DATA_DELAY


def test_window_stops_one_second_before_the_hour() -> None:
    """Mid-session the forming bar is excluded, not truncated into the table."""
    now = _at(MONDAY, 11, 20)
    assert _session_end(MONDAY, now) == _at(MONDAY, 10).replace(
        minute=59, second=59
    )


@pytest.mark.parametrize(
    "now, expected",
    [
        # Pre-market opens 04:00 ET; the 04:00 bar is readable at 05:15.
        (_at(TUESDAY, 4, 20), MONDAY.date()),
        (_at(TUESDAY, 5, 14), MONDAY.date()),
        (_at(TUESDAY, 5, 20), TUESDAY.date()),
        (_at(TUESDAY, 9, 35), TUESDAY.date()),
        (_at(TUESDAY, 11, 20), TUESDAY.date()),
        # After the 20:00 extended close the same session stays the target.
        (_at(MONDAY, 20, 20), MONDAY.date()),
        (_at(MONDAY, 21, 20), MONDAY.date()),
        # Past New York midnight the closed session is the target again.
        (_at(TUESDAY, 0, 30), MONDAY.date()),
        (_at(TUESDAY, 3, 0), MONDAY.date()),
        # Weekends and the pre-market gap on Monday look back to Friday.
        (_at(datetime(2026, 10, 31), 12, 0), FRIDAY.date()),
        (_at(MONDAY, 2, 0), datetime(2026, 10, 23).date()),
    ],
)
def test_target_session(now: datetime, expected) -> None:
    assert _target_session(now) == expected


@pytest.mark.parametrize(
    "now, hour, minute",
    [
        (_at(MONDAY, 9, 35), 8, 0),
        (_at(MONDAY, 11, 20), 10, 0),
        (_at(MONDAY, 16, 20), 15, 0),
        (_at(MONDAY, 17, 20), 16, 0),
        # Extended hours end at 20:00, so the session's last bar opens at 19:00
        # and stays the target after the close.
        (_at(MONDAY, 20, 20), 19, 0),
        (_at(MONDAY, 21, 20), 19, 0),
        (_at(TUESDAY, 0, 30), 19, 0),
    ],
)
def test_newest_readable_bar(now: datetime, hour: int, minute: int) -> None:
    target = _target_session(now)
    end = _session_end(target, now)
    newest = datetime.fromtimestamp(_expected_last_open(target, end), NY)
    assert (newest.hour, newest.minute) == (hour, minute)
    assert newest.date() == target


def test_completed_session_window_covers_the_whole_day() -> None:
    """A look-back at an earlier session asks for its 23:59:59, not today's cut."""
    now = _at(TUESDAY, 8, 0)
    end = _session_end(MONDAY.date(), now)
    assert end == datetime(2026, 10, 26, 23, 59, 59, tzinfo=NY)


def test_first_bar_hour_matches_extended_open() -> None:
    """04:00 ET is the first extended-hours bar; keep the constant honest."""
    assert _FIRST_BAR_HOUR == 4
    now = _at(TUESDAY, 5, 20)
    end = _session_end(TUESDAY.date(), now)
    newest = datetime.fromtimestamp(_expected_last_open(TUESDAY.date(), end), NY)
    assert newest.hour == _FIRST_BAR_HOUR
