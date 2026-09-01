from __future__ import annotations

from datetime import datetime, timezone

from core.backtester import BacktestTrade, _check_exit
from core.paper_trader import PaperAccount


def _short(qty: float = 10, entry: float = 100.0) -> BacktestTrade:
    return BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="test", action="SELL",
        entry_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        entry_price=entry, exit_price=entry, pnl=0.0, pnl_pct=0.0, qty=qty,
    )


def test_short_open_keeps_equity_flat():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = _short()
    acct.positions["TEST"] = t
    acct._last_price["TEST"] = t.entry_price
    # Mirror _open_position_locked: a short receives its sale proceeds up
    # front, but the short liability must offset those proceeds in equity.
    acct.cash += t.entry_price * t.qty
    assert acct.equity() == 100_000.0


def test_short_mark_to_market():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = _short()
    acct.positions["TEST"] = t
    acct._last_price["TEST"] = t.entry_price
    acct.cash += t.entry_price * t.qty

    # Price falls 100 -> 90: short gains (entry - current) * qty.
    acct._last_price["TEST"] = 90.0
    assert acct.equity() == 100_000.0 + (100.0 - 90.0) * t.qty

    # Price rises 100 -> 110: short loses (current - entry) * qty.
    acct._last_price["TEST"] = 110.0
    assert acct.equity() == 100_000.0 - (110.0 - 100.0) * t.qty


def test_daily_loss_reset_uses_market_timezone():
    from zoneinfo import ZoneInfo
    from datetime import timedelta

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    # 00:30 UTC is 20:30 ET during DST: still the same US session date.
    ts = datetime(2026, 8, 17, 0, 30, tzinfo=timezone.utc)
    acct._daily_key = "2026-08-16"
    acct._daily_pnl = -100.0
    acct._reset_daily_if_needed(ts)
    assert acct._daily_key == "2026-08-16"
    assert acct._daily_pnl == -100.0


def test_processed_bar_identity_persists():
    acct = PaperAccount(initial_capital=100_000.0, market="us")
    acct.mark_bar_processed("AAPL", "1d", "datetime.date(2026, 8, 17)")
    assert acct.processed_bar_identities_snapshot()["AAPL|1d"] == "datetime.date(2026, 8, 17)"


def test_time_exit_records_signal_elapsed_bars():
    t = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="pattern_007_descending_channel",
        action="BUY",
        entry_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0,
        qty=10, entry_bar_idx=101,
        neckline=99.0, neckline_break_direction="above",
        neckline_break_bar_idx=100, exit_bars_after_neckline_break=15,
    )
    candle = type("Candle", (), {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
    })()

    fill, reason = _check_exit(candle, t, 114)
    assert fill is None
    assert reason == ""

    fill, reason = _check_exit(candle, t, 115)
    assert fill == 100.0
    assert reason == "time_exit"
    assert t.time_exit_bars_elapsed == 15
    assert t.exit_bar_idx == 115
    assert 115 - t.entry_bar_idx == 14


def test_time_exit_skips_winners_when_unfavorable_only():
    t = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="pattern_003_double_bottom",
        action="BUY",
        entry_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0,
        qty=10, entry_bar_idx=101,
        neckline=99.0, neckline_break_direction="above",
        neckline_break_bar_idx=100, exit_bars_after_neckline_break=15,
        time_exit_only_unfavorable=True,
    )
    winner = type("Candle", (), {
        "open": 102.0, "high": 103.0, "low": 101.0, "close": 102.0,
    })()
    fill, reason = _check_exit(winner, t, 115)
    assert fill is None
    assert reason == ""

    loser = type("Candle", (), {
        "open": 99.0, "high": 99.5, "low": 98.0, "close": 98.5,
    })()
    fill, reason = _check_exit(loser, t, 115)
    assert fill == 98.5
    assert reason == "time_exit"


def test_unfavorable_time_exit_fires_at_eight_bars():
    t = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="pattern_007_descending_channel",
        action="BUY",
        entry_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0,
        qty=10, entry_bar_idx=101,
        neckline=99.0, neckline_break_direction="above",
        neckline_break_bar_idx=100, exit_bars_after_neckline_break=8,
        time_exit_only_unfavorable=True,
    )
    underwater = type("Candle", (), {
        "open": 99.0, "high": 99.5, "low": 98.0, "close": 98.5,
    })()
    fill, reason = _check_exit(underwater, t, 107)
    assert fill is None
    fill, reason = _check_exit(underwater, t, 108)
    assert fill == 98.5
    assert reason == "time_exit"

    winner = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="pattern_007_descending_channel",
        action="BUY",
        entry_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0,
        qty=10, entry_bar_idx=101,
        neckline=99.0, neckline_break_direction="above",
        neckline_break_bar_idx=100, exit_bars_after_neckline_break=8,
        time_exit_only_unfavorable=True,
    )
    ahead = type("Candle", (), {
        "open": 102.0, "high": 103.0, "low": 101.0, "close": 102.0,
    })()
    fill, reason = _check_exit(ahead, winner, 108)
    assert fill is None
    assert reason == ""


def test_mark_to_market_deduplicates_session_marks():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.mark_to_market(datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc))
    acct.mark_to_market(datetime(2026, 8, 17, 19, 55, tzinfo=timezone.utc))
    assert len(acct.equity_curve_snapshot()) == 1


def test_open_position_sim_age_uses_simulated_clock():
    from core.paper_trader import sim_days_held, bars_held

    t = _short()
    t.entry_bar_idx = 10
    t.sim_entry_date = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert sim_days_held(t, datetime(2026, 8, 15, tzinfo=timezone.utc)) == 14.0
    assert bars_held(t, 24) == 14


def test_result_exposes_r_and_exit_breakdown():
    from core.backtester import BacktestResult

    t = _short()
    t.pnl = 2.0
    t.qty = 10
    t.exit_reason = "time_exit"
    t.entry_bar_idx = 10
    t.exit_bar_idx = 20
    result = BacktestResult(trades=[t], initial_capital=100_000.0)
    assert result.avg_hold_bars == 10.0
    assert result.exit_reason_breakdown == {"time_exit": 1}
    assert result.to_dict()["exit_reason_breakdown"] == {"time_exit": 1}


def test_bar_counters_are_isolated_by_timeframe():
    from core.paper_trader import PaperAccount
    from data.tv_client import OHLCVCandle

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    daily = OHLCVCandle(
        open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000,
        timestamp=datetime(2026, 8, 17, tzinfo=timezone.utc),
    )
    weekly = OHLCVCandle(
        open=100.0, high=102.0, low=98.0, close=101.0, volume=5_000,
        timestamp=datetime(2026, 8, 17, tzinfo=timezone.utc),
    )

    acct.on_bar("TEST", daily, "1d", True)
    acct.on_bar("TEST", weekly, "1W", True)

    assert acct.bar_count("TEST", "1d") == 1
    assert acct.bar_count("TEST", "1W") == 1
    assert acct.bar_count("TEST", "1h") == 0


def test_paper_closes_open_long_when_giveback_hits_profit_lock():
    from data.tv_client import OHLCVCandle

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = BacktestTrade(
        symbol="AAPL", timeframe="1d", pattern="test", action="BUY",
        entry_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0, qty=10,
        stop_loss=90.0, take_profit=120.0, entry_bar_idx=0,
    )
    acct.positions["AAPL"] = t
    acct._last_price["AAPL"] = 100.0
    acct.cash -= 1_000.0
    # Bar 1: establish +10% peak close-to-close MFE (same-bar arming;
    # lock floors the subsequent giveback bar).
    peak = OHLCVCandle(
        open=108, high=111, low=107, close=110.0, volume=1,
        timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    assert acct.on_bar("AAPL", peak, "1d", True) is None
    assert t._best_pnl_pct == 0.10
    # Bar 2: giveback through the 50% lock floor at 105.
    giveback = OHLCVCandle(
        open=106, high=107, low=103, close=104.0, volume=1,
        timestamp=datetime(2026, 1, 6, tzinfo=timezone.utc),
    )
    closed = acct.on_bar("AAPL", giveback, "1d", True)
    assert closed is not None
    assert closed.exit_reason == "profit_lock"
    assert "AAPL" not in acct.positions


def test_sim_clock_isolated_by_timeframe():
    from core.paper_trader import PaperAccount

    acct = PaperAccount(initial_capital=100_000.0, market="us")
    daily_ts = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
    weekly_ts = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)

    acct._sim_now_by_timeframe["1d"] = daily_ts
    acct._sim_now_by_timeframe["1W"] = weekly_ts
    acct._sim_now = weekly_ts

    assert acct.sim_now("1d") == daily_ts
    assert acct.sim_now("1W") == weekly_ts
    assert acct.sim_now() == weekly_ts


class _EmptyStore:
    def get_df(self, symbol, timeframe, min_bars=1):
        return None


def test_pattern_cap_and_min_notional_skip_fill():
    from config import settings
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal

    candle = OHLCVCandle(
        open=100, high=100, low=100, close=100, volume=1,
        timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    def _buy(symbol: str) -> TradeSignal:
        return TradeSignal(
            symbol=symbol, timeframe="1d", pattern="test",
            action="BUY", price=100.0, confidence=0.9, qty=10,
            stop_loss=94.0, take_profit=120.0,
        )

    old_cap = settings.max_open_per_pattern
    old_min = settings.min_position_notional
    try:
        settings.max_open_per_pattern = 1
        settings.min_position_notional = 0
        acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
        acct.positions["AAA"] = _short()
        ok, reason = acct.open_position(_buy("BBB"), candle, _EmptyStore())
        assert not ok, reason
        assert "Pattern cap" in reason

        settings.max_open_per_pattern = 0
        settings.min_position_notional = 1_000_000_000
        acct2 = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
        ok, reason = acct2.open_position(_buy("CCC"), candle, _EmptyStore())
        assert not ok, reason
        assert "Min notional" in reason
    finally:
        settings.max_open_per_pattern = old_cap
        settings.min_position_notional = old_min


def test_ph_stream_ignores_wall_clock_session():
    from unittest.mock import patch

    from config import settings
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal

    candle = OHLCVCandle(
        open=57.6, high=57.7, low=57.5, close=57.65, volume=1_000,
        timestamp=datetime(2026, 1, 6, tzinfo=timezone.utc),
    )
    signal = TradeSignal(
        symbol="PNB", timeframe="1d", pattern="test",
        action="BUY", price=57.65, confidence=0.9, qty=10,
        stop_loss=54.0, take_profit=70.0,
    )
    old_min = settings.min_position_notional
    try:
        settings.min_position_notional = 0
        live = PaperAccount(initial_capital=1_000_000.0, market="ph", slippage_pct=0.0)
        live.assume_session_open = False
        with patch("core.paper_trader.may_assume_fill", return_value=False):
            ok, reason = live.open_position(signal, candle, _EmptyStore())
        assert not ok
        assert "Session closed" in reason

        replay = PaperAccount(initial_capital=1_000_000.0, market="ph", slippage_pct=0.0)
        replay.assume_session_open = True
        with patch("core.paper_trader.may_assume_fill", return_value=False):
            ok, reason = replay.open_position(signal, candle, _EmptyStore())
        assert ok, reason
        assert "PNB" in replay.positions
        pos = replay.positions["PNB"]
        assert pos.breakeven_trigger_pct == 0.05
        assert pos.breakeven_buffer_pct == 0.008
    finally:
        settings.min_position_notional = old_min


def test_us_paper_keeps_engine_breakeven():
    from config import settings
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal

    candle = OHLCVCandle(
        open=100, high=100, low=100, close=100, volume=1,
        timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    signal = TradeSignal(
        symbol="AAPL", timeframe="1d", pattern="test",
        action="BUY", price=100.0, confidence=0.9, qty=10,
        stop_loss=94.0, take_profit=120.0,
    )
    old_min = settings.min_position_notional
    try:
        settings.min_position_notional = 0
        acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
        ok, reason = acct.open_position(signal, candle, _EmptyStore())
        assert ok, reason
        pos = acct.positions["AAPL"]
        assert pos.breakeven_trigger_pct == 0.06
        assert pos.breakeven_buffer_pct == 0.0015
        assert pos.profit_take_pct is None
        assert pos.profit_lock_frac == 0.5
        # entry=100, stop=94 → risk_pct=0.06; trigger_r=0.4 → resolved
        # per-trade trigger = 0.4 * 0.06 = 0.024 (arm after +0.4R).
        assert pos.profit_lock_trigger_pct == 0.024
    finally:
        settings.min_position_notional = old_min


def test_pattern_only_allows_ph_short_fill():
    from config import settings
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal

    candle = OHLCVCandle(
        open=57.6, high=57.7, low=57.5, close=57.65, volume=1_000,
        timestamp=datetime(2026, 1, 6, tzinfo=timezone.utc),
    )
    signal = TradeSignal(
        symbol="PNB", timeframe="1d", pattern="test",
        action="SELL", price=57.65, confidence=0.9, qty=10,
        stop_loss=62.0, take_profit=50.0,
    )
    old_min = settings.min_position_notional
    try:
        settings.min_position_notional = 0
        blocked = PaperAccount(initial_capital=1_000_000.0, market="ph", slippage_pct=0.0)
        blocked.assume_session_open = True
        blocked.pattern_only = False
        ok, reason = blocked.open_position(signal, candle, _EmptyStore())
        assert not ok
        assert "Long-only" in reason

        allowed = PaperAccount(initial_capital=1_000_000.0, market="ph", slippage_pct=0.0)
        allowed.assume_session_open = True
        allowed.pattern_only = True
        ok, reason = allowed.open_position(signal, candle, _EmptyStore())
        assert ok, reason
        assert allowed.positions["PNB"].action == "SELL"
    finally:
        settings.min_position_notional = old_min


def test_position_marks_record_entry_and_each_session_bar():
    from data.tv_client import OHLCVCandle

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = BacktestTrade(
        symbol="AAPL", timeframe="1d", pattern="test", action="BUY",
        entry_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
        exit_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0, qty=10,
        stop_loss=94.0, take_profit=120.0, entry_bar_idx=0,
        sim_entry_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
    )
    acct.positions["AAPL"] = t
    acct._last_price["AAPL"] = 100.0
    acct.cash -= 1_000.0
    acct._record_position_mark(
        "AAPL", t, 100.0, datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc), 0,
    )
    assert len(t.position_marks) == 1
    assert t.position_marks[0]["date"] == "2026-08-18"
    assert t.position_marks[0]["close"] == 100.0

    bar1 = OHLCVCandle(
        open=101.0, high=102.0, low=100.5, close=101.5, volume=1_000,
        timestamp=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
    )
    bar1_late = OHLCVCandle(
        open=101.0, high=102.0, low=100.5, close=101.8, volume=1_000,
        timestamp=datetime(2026, 8, 19, 23, 0, tzinfo=timezone.utc),
    )
    bar2 = OHLCVCandle(
        open=102.0, high=103.0, low=101.0, close=102.5, volume=1_000,
        timestamp=datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )
    acct.on_bar("AAPL", bar1, "1d", True)
    assert len(t.position_marks) == 2
    assert t.position_marks[-1]["date"] == "2026-08-19"
    assert t.position_marks[-1]["close"] == 101.5

    acct.on_bar("AAPL", bar1_late, "1d", True)
    assert len(t.position_marks) == 2
    assert t.position_marks[-1]["close"] == 101.8

    acct.on_bar("AAPL", bar2, "1d", True)
    assert len(t.position_marks) == 3
    assert t.position_marks[-1]["date"] == "2026-08-20"
    assert t.position_marks[-1]["unrl_pct"] == 2.5


def test_position_marks_record_exit_fill_not_bar_close():
    from data.tv_client import OHLCVCandle

    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = BacktestTrade(
        symbol="AAPL", timeframe="1d", pattern="test", action="BUY",
        entry_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
        exit_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
        entry_price=100.0, exit_price=100.0, pnl=0.0, pnl_pct=0.0, qty=10,
        stop_loss=90.0, take_profit=120.0, entry_bar_idx=0,
        sim_entry_date=datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
    )
    acct.positions["AAPL"] = t
    acct._last_price["AAPL"] = 100.0
    acct.cash -= 1_000.0
    acct._record_position_mark(
        "AAPL", t, 100.0, datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc), 0,
    )
    # Close is green; the low tagged the hard stop. Last mark must be the
    # fill, not the +8.8% close that made the trade look like a winner.
    dump = OHLCVCandle(
        open=103.0, high=109.0, low=89.0, close=108.8, volume=1_000,
        timestamp=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
    )
    closed = acct.on_bar("AAPL", dump, "1d", True)
    assert closed is not None
    assert closed.exit_reason == "stop_loss"
    assert t.position_marks[-1]["close"] == 90.0
    assert t.position_marks[-1]["status"] == "stop_loss"
    assert t.position_marks[-1]["unrl_pct"] == -10.0

