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
