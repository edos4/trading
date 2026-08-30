from datetime import datetime, timezone
from types import SimpleNamespace

from utils.trade_display import (
    closed_book_stats,
    format_closed_stats,
    format_exit_reason,
    format_hold,
    format_pattern_name,
    format_stamp,
    pnl_dollars,
    reason_tone,
)


def test_format_pattern_name_strips_prefix():
    assert format_pattern_name("pattern_003_double_bottom") == "003 double bottom"
    assert format_pattern_name("pattern_008_head_and_shoulders") == "008 head and shoulders"
    assert format_pattern_name("") == "—"


def test_format_exit_reason_humanizes_and_adds_time_bars():
    assert format_exit_reason("stop_loss") == "Stop"
    assert format_exit_reason("take_profit") == "Target"
    assert format_exit_reason("profit_take") == "Lock"
    assert format_exit_reason("trailing_stop") == "Trail"
    assert format_exit_reason("time_exit", 14) == "Time 14b"
    assert format_exit_reason("time_exit", 14, 20) == "Time 14/20b"
    assert format_exit_reason("breakeven_stop") == "BE"
    assert reason_tone("stop_loss") == "loss"
    assert reason_tone("take_profit") == "gain"
    assert reason_tone("profit_take") == "gain"


def test_format_hold_and_stamp():
    assert format_hold(2.0, 10) == "2.0d · 10b"
    assert format_hold(0.25, 2) == "6.0h · 2b"
    assert format_hold(None, None) == "—"
    dt = datetime(2026, 8, 17, 18, 33, 4, tzinfo=timezone.utc)
    assert format_stamp(dt) == "08-17 18:33"
    assert format_stamp(None) == "—"


def test_closed_book_stats_last_ten():
    trades = []
    for i in range(12):
        trades.append(
            SimpleNamespace(
                qty=10,
                pnl=1.0 if i % 3 == 0 else -0.5,
                exit_date=datetime(2026, 8, 1, i, tzinfo=timezone.utc),
                exit_price=100.0,
            )
        )
    s = closed_book_stats(trades)
    assert s["count"] == 12
    assert s["wins"] == 4
    assert s["losses"] == 8
    assert s["last_n"] == 10
    assert abs(pnl_dollars(trades[0]) - 10.0) < 1e-9
    line = format_closed_stats(trades, money="+$12.00", showing=5)
    assert "12 closed" in line
    assert "showing 5" in line
    assert format_closed_stats([]) == "No closed trades yet."
