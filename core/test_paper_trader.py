from __future__ import annotations

from datetime import datetime, timezone

from core.backtester import BacktestTrade, _check_exit
from core.paper_trader import PaperAccount
from data.tv_client import OHLCVCandle
from patterns.base_pattern import TradeSignal


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
    acct.positions["TEST"] = [t]
    acct._last_price["TEST"] = t.entry_price
    # Mirror _open_position_locked: a short receives its sale proceeds up
    # front, but the short liability must offset those proceeds in equity.
    acct.cash += t.entry_price * t.qty
    assert acct.equity() == 100_000.0


def test_short_mark_to_market():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    t = _short()
    acct.positions["TEST"] = [t]
    acct._last_price["TEST"] = t.entry_price
    acct.cash += t.entry_price * t.qty

    # Price falls 100 -> 90: short gains (entry - current) * qty.
    acct._last_price["TEST"] = 90.0
    assert acct.equity() == 100_000.0 + (100.0 - 90.0) * t.qty

    # Price rises 100 -> 110: short loses (current - entry) * qty.
    acct._last_price["TEST"] = 110.0
    assert acct.equity() == 100_000.0 - (110.0 - 100.0) * t.qty


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


def _bar(px: float, day: int = 5, high=None, low=None) -> OHLCVCandle:
    return OHLCVCandle(
        open=px, high=high if high is not None else px,
        low=low if low is not None else px, close=px, volume=1_000,
        timestamp=datetime(2026, 3, day, tzinfo=timezone.utc),
    )


def _sig(pattern: str, action: str, px: float, key, **kw) -> TradeSignal:
    base = dict(
        symbol="NVDA", action=action, pattern=pattern, timeframe="1d",
        confidence=1.0, price=px, qty=0.0, setup_key=tuple(key),
    )
    base.update(kw)
    return TradeSignal(**base)


def test_paper_carries_multiple_independent_anchors_per_symbol():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.assume_session_open = True
    store = _EmptyStore()
    c = _bar(100.0)

    ok, _ = acct.open_position(_sig("pattern_002_double_top", "SELL", 100.0, (10, 40)), c, store)
    assert ok
    ok, _ = acct.open_position(_sig("pattern_008_head_and_shoulders", "SELL", 100.0, (12, 55)), c, store)
    assert ok
    ok, _ = acct.open_position(_sig("pattern_006_upward_channel", "SELL", 100.0, (5, 33)), c, store)
    assert ok
    assert acct.open_count() == 3
    assert len(acct.open_for("NVDA")) == 3

    # same (pattern, setup_key) again -> rejected as a duplicate anchor
    ok, msg = acct.open_position(_sig("pattern_002_double_top", "SELL", 100.0, (10, 40)), c, store)
    assert not ok and "Duplicate anchor" in msg
    assert acct.open_count() == 3


def test_paper_respects_max_open_per_symbol():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.assume_session_open = True
    store = _EmptyStore()
    c = _bar(50.0)

    ok, _ = acct.open_position(_sig("pattern_009_flag_pattern", "BUY", 50.0, (1, 2, 3)), c, store)
    assert ok
    ok, msg = acct.open_position(_sig("pattern_009_flag_pattern", "BUY", 50.0, (4, 5, 6)), c, store)
    assert not ok and "MAX_OPEN_PER_SYMBOL" in msg
    assert acct.open_count() == 1


def test_paper_on_bar_closes_every_exiting_anchor_on_one_bar():
    acct = PaperAccount(initial_capital=100_000.0, market="us", slippage_pct=0.0)
    acct.assume_session_open = True
    store = _EmptyStore()
    acct.open_position(
        _sig("pattern_002_double_top", "SELL", 100.0, (10, 40),
             stop_loss=105.0, stop_loss_on_close=True), _bar(100.0), store)
    acct.open_position(
        _sig("pattern_008_head_and_shoulders", "SELL", 100.0, (12, 55),
             stop_loss=106.0, stop_loss_on_close=True), _bar(100.0), store)
    assert acct.open_count() == 2

    # a gap-up close above both invalidation stops exits both on the same bar
    closed = acct.on_bar("NVDA", _bar(110.0, day=6), "1d", True)
    assert len(closed) == 2
    assert {t.pattern for t in closed} == {
        "pattern_002_double_top", "pattern_008_head_and_shoulders",
    }
    assert acct.open_count() == 0
    assert len(acct.closed) == 2


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
    acct.positions["AAPL"] = [t]
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
    acct.positions["AAPL"] = [t]
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
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert t.position_marks[-1]["close"] == 90.0
    assert t.position_marks[-1]["status"] == "stop_loss"
    assert t.position_marks[-1]["unrl_pct"] == -10.0

