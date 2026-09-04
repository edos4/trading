from __future__ import annotations

from datetime import datetime, timezone

from core.backtester import (
    BacktestTrade,
    _apply_capital_ledger,
    _check_exit,
)
from data.tv_client import OHLCVCandle


def _candle(open_: float, high: float, low: float, close: float) -> OHLCVCandle:
    return OHLCVCandle(
        open=open_, high=high, low=low, close=close, volume=1_000.0,
        timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def _trade(action: str, entry: float, stop: float | None, target: float | None, qty: float = 10) -> BacktestTrade:
    return BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="test", action=action,
        entry_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        exit_date=datetime(2026, 1, 3, tzinfo=timezone.utc),
        entry_price=entry, exit_price=entry, pnl=0.0, pnl_pct=0.0,
        stop_loss=stop, take_profit=target, qty=qty,
    )


def test_long_stop_gap_fills_at_open():
    trade = _trade("BUY", 100, 95, None)
    price, reason = _check_exit(_candle(88, 92, 85, 90), trade, 2)
    assert reason == "stop_loss"
    assert price == 88


def test_short_stop_gap_fills_at_open():
    trade = _trade("SELL", 100, 105, None)
    price, reason = _check_exit(_candle(112, 115, 108, 110), trade, 2)
    assert reason == "stop_loss"
    assert price == 112


def test_long_target_gap_fills_at_open():
    trade = _trade("BUY", 100, None, 105)
    price, reason = _check_exit(_candle(110, 112, 109, 111), trade, 2)
    assert reason == "take_profit"
    assert price == 110


def test_short_target_gap_fills_at_open():
    trade = _trade("SELL", 100, None, 95)
    price, reason = _check_exit(_candle(90, 92, 88, 89), trade, 2)
    assert reason == "take_profit"
    assert price == 90


def test_long_profit_take_closes_at_four_percent():
    trade = _trade("BUY", 100, 90, 120)
    trade.profit_take_pct = 0.04
    price, reason = _check_exit(_candle(103, 105, 102, 104.5), trade, 2)
    assert reason == "profit_take"
    assert price == 104


def test_short_profit_take_closes_at_four_percent():
    trade = _trade("SELL", 100, 110, 80)
    trade.profit_take_pct = 0.04
    price, reason = _check_exit(_candle(97, 98, 95, 95.5), trade, 2)
    assert reason == "profit_take"
    assert price == 96


def test_profit_take_does_not_fire_under_four_percent():
    trade = _trade("BUY", 100, 90, 120)
    trade.profit_take_pct = 0.04
    price, reason = _check_exit(_candle(102, 103.9, 101, 103), trade, 2)
    assert reason == ""
    assert price is None


def test_long_profit_lock_floors_half_of_peak_unrl():
    # Peak close-to-close +10% → lock floor at +5% with frac=0.5.
    trade = _trade("BUY", 100, 90, 120)
    trade.profit_lock_frac = 0.5
    trade._best_pnl_pct = 0.10
    trade.entry_bar_idx = 0
    price, reason = _check_exit(_candle(106, 107, 103, 104), trade, 2)
    assert reason == "profit_lock"
    assert price == 105


def test_short_profit_lock_floors_half_of_peak_unrl():
    trade = _trade("SELL", 100, 110, 80)
    trade.profit_lock_frac = 0.5
    trade._best_pnl_pct = 0.10
    trade.entry_bar_idx = 0
    price, reason = _check_exit(_candle(94, 97, 93, 96), trade, 2)
    assert reason == "profit_lock"
    assert price == 95


def test_profit_lock_does_not_cap_further_upside():
    # Still running above the lock floor — no exit; pattern target/trail handle
    # the rest. Hard profit_take stays off.
    trade = _trade("BUY", 100, 90, 120)
    trade.profit_lock_frac = 0.5
    trade._best_pnl_pct = 0.10
    trade.entry_bar_idx = 0
    price, reason = _check_exit(_candle(108, 112, 107, 111), trade, 2)
    assert reason == ""
    assert price is None


def test_profit_lock_ignores_sub_trigger_noise():
    # 2026-08-30 paper: PH/BDX/OKE locked +0.3–0.5% after ~1% MFE.
    trade = _trade("BUY", 100, 90, 120)
    trade.profit_lock_frac = 0.5
    trade.profit_lock_trigger_pct = 0.03
    trade._best_pnl_pct = 0.013
    trade.entry_bar_idx = 0
    price, reason = _check_exit(_candle(100.8, 101.2, 100.2, 100.4), trade, 2)
    assert reason == ""
    assert price is None


def test_profit_lock_beats_breakeven_when_both_crossed():
    # MFE +10% → lock at 105; BE armed at ~100.15. Low=103 crosses lock
    # not BE, so the first floor from the open is the lock.
    trade = _trade("BUY", 100, 90, None)
    trade.profit_lock_frac = 0.5
    trade._best_pnl_pct = 0.10
    trade.breakeven_trigger_pct = 0.03
    trade.breakeven_buffer_pct = 0.0015
    trade.entry_bar_idx = 0
    price, reason = _check_exit(_candle(106, 107, 103, 104), trade, 2)
    assert reason == "profit_lock"
    assert price == 105


def test_short_ledger_uses_equity_not_short_sale_proceeds():
    trade = _trade("SELL", 100, 110, 90, qty=100)
    accepted, rejected = _apply_capital_ledger(
        [trade],
        initial_capital=100_000,
        risk_per_trade_pct=0.01,
        position_sizing="notional",
        max_position_pct=0.02,
        max_gross_exposure_pct=1.0,
    )
    assert rejected == 0
    assert accepted == [trade]
    # 2% of $100k, not 2% of the inflated cash balance after short-sale proceeds.
    assert trade.qty == 20


def test_ledger_accounts_for_transaction_costs_on_both_legs():
    # 2% notional on $100k at $100 = 20 shares. BacktestTrade.pnl is a
    # per-share figure (net of entry + exit txn costs), independent of qty —
    # _apply_capital_ledger resizes qty but must not rescale per-share pnl.
    trade = _trade("BUY", 100, 90, 120, qty=20)
    trade.exit_price = 120
    trade.pnl = 20.0 - (100.0 + 120.0) * 0.001
    accepted, rejected = _apply_capital_ledger(
        [trade], 100_000, 0.01, "notional", 0.02,
        max_gross_exposure_pct=1.0, txn_cost_pct=0.001,
    )
    assert rejected == 0
    assert accepted == [trade]
    assert trade.qty == 20
    # Realized capital includes both entry and exit fees on the resized qty.
    expected = 100_000 + (120 - 100) * 20 - (100 + 120) * 20 * 0.001
    actual = 100_000 + trade.pnl * trade.qty
    assert abs(actual - expected) < 1e-9


def test_long_multiple_stops_fill_first_floor_from_open():
    trade = _trade("BUY", 100, 95, None)
    trade.breakeven_trigger_pct = 0.0
    trade._best_pnl_pct = 0.0
    # Open above both levels, low crossing BE (100.15) then the hard stop.
    # First floor from the open is breakeven, not the catastrophe stop.
    price, reason = _check_exit(_candle(102, 103, 93, 94), trade, 2)
    assert reason == "breakeven_stop"
    assert price == 100.15


def test_short_multiple_stops_fill_first_floor_from_open():
    trade = _trade("SELL", 100, 105, None)
    trade.breakeven_trigger_pct = 0.0
    trade._best_pnl_pct = 0.0
    # Open below both levels, high crossing BE (99.85) then the hard stop.
    # First ceiling from the open is breakeven, not the catastrophe stop.
    price, reason = _check_exit(_candle(98, 107, 97, 106), trade, 2)
    assert reason == "breakeven_stop"
    assert abs(price - 99.85) < 1e-9


def test_same_bar_peak_close_arms_trail_not_hard_stop():
    # AVNW-style: close +8.8% would have been a winner; the low tagged the
    # 10% hard stop. Same-bar arming + first-floor fill keeps the trail.
    trade = _trade("BUY", 100, 90, 120)
    trade.trailing_stop_pct = 0.025
    trade.trailing_stop_mode = "highest_low"
    trade.trailing_activation_pct = 0.04
    trade.entry_bar_idx = 0
    trade.highest_low_since_entry = 103.5
    price, reason = _check_exit(_candle(103.5, 109, 89, 108.8), trade, 3)
    assert reason == "trailing_stop"
    assert abs(price - 108.8 * 0.975) < 1e-9


def test_prearmed_trail_gap_still_fills_at_open():
    trade = _trade("BUY", 100, 90, 120)
    trade.trailing_stop_pct = 0.025
    trade.trailing_stop_mode = "highest_low"
    trade.trailing_activation_pct = 0.04
    trade._trailing_activated = True
    trade._best_pnl_pct = 0.0546
    trade.highest_low_since_entry = 105.46
    trade.entry_bar_idx = 0
    # Prior close 105.46 → trail 102.82. Open 101 gaps through that floor.
    price, reason = _check_exit(_candle(101, 102, 100.5, 101.2), trade, 3)
    assert reason == "trailing_stop"
    assert price == 101


def test_signal_bar_neckline_time_stop_starts_on_signal_bar():
    from core.backtester import _open_trade
    from patterns.base_pattern import TradeSignal

    signal = TradeSignal(
        symbol="TEST", action="BUY", pattern="pattern_007_descending_channel",
        timeframe="1d", confidence=0.9, price=100.0, qty=10,
        stop_loss=90.0, take_profit=120.0,
        neckline=99.0, neckline_break_direction="above",
        exit_bars_after_neckline_break=3,
        signal_bar_idx=10,
    )
    fill = OHLCVCandle(
        open=101, high=105, low=100, close=102, volume=1000,
        timestamp=datetime(2026, 1, 12, tzinfo=timezone.utc),
    )
    position = _open_trade(signal, fill, bar_idx=10)
    assert position.entry_bar_idx == 10
    assert position.neckline_break_bar_idx == 10

    # Three bars after the signal bar is the time-stop on signal-bar entry.
    later = _candle(102, 103, 101, 102)
    price, reason = _check_exit(
        later, position, bar_idx=13, min_hold_bars=0, dead_trade_flatten_bars=0,
    )
    assert reason == "time_exit"
    assert price == 102


def test_first_bar_invalidation_exits_on_bar_one_close():
    # stop is 10% below entry; a -4% close burns 40% of that planned risk —
    # comfortably past the 30% min_risk_fraction floor — so this still
    # reads as a genuine same-day reversal, not noise.
    trade = _trade("BUY", 100.0, 90.0, 120.0)
    trade.entry_bar_idx = 5
    candle = _candle(97.0, 97.5, 95.5, 96.0)
    price, reason = _check_exit(candle, trade, bar_idx=6, min_hold_bars=2)
    assert reason == "first_bar_invalidation"
    assert price == 96.0


def test_first_bar_invalidation_skips_favorable_bar_one_close():
    trade = _trade("BUY", 100.0, 90.0, 120.0)
    trade.entry_bar_idx = 5
    candle = _candle(100.5, 101.0, 100.0, 100.5)
    price, reason = _check_exit(candle, trade, bar_idx=6, min_hold_bars=2)
    assert reason != "first_bar_invalidation"


def test_first_bar_invalidation_spares_noise_on_a_wide_stop():
    """2026-09-02 review: a -1.5% close is only 15% of a 10%-wide stop's
    planned risk — below the 30% min_risk_fraction floor — so a wide-stop
    swing setup gets to see its actual stop/target instead of being killed
    by a move that's small relative to what it was already sized to take.
    """
    trade = _trade("BUY", 100.0, 90.0, 120.0)
    trade.entry_bar_idx = 5
    candle = _candle(99.5, 100.0, 99.0, 98.5)
    price, reason = _check_exit(candle, trade, bar_idx=6, min_hold_bars=2)
    assert reason != "first_bar_invalidation"
    assert price is None


def test_first_bar_invalidation_still_fast_on_a_tight_stop():
    """A tight 3%-wide stop should still invalidate on a small absolute
    move once that move clears 30% of ITS OWN (smaller) risk budget —
    the risk-fraction floor must not make invalidation universally looser.
    """
    trade = _trade("BUY", 100.0, 97.0, 106.0)
    trade.entry_bar_idx = 5
    candle = _candle(99.3, 99.5, 98.8, 99.0)
    price, reason = _check_exit(candle, trade, bar_idx=6, min_hold_bars=2)
    assert reason == "first_bar_invalidation"
    assert price == 99.0


def test_dead_trade_exit_flattens_at_bar_three_without_mfe():
    trade = _trade("BUY", 100.0, 90.0, 120.0)
    trade.entry_bar_idx = 5
    trade._best_pnl_pct = 0.001
    candle = _candle(99.0, 99.5, 98.5, 99.0)
    price, reason = _check_exit(candle, trade, bar_idx=8, min_hold_bars=2)
    assert reason == "dead_trade_exit"
    assert price == 99.0


def _time_exit_trade(action: str, best_mfe_pct: float) -> BacktestTrade:
    trade = _trade(action, 100.0, None, None)
    trade.entry_bar_idx = 5
    trade.neckline_break_bar_idx = 10
    trade.exit_bars_after_neckline_break = 3
    trade.time_exit_only_unfavorable = True
    trade.time_exit_min_mfe_pct = 0.02
    trade._best_pnl_pct = best_mfe_pct
    return trade


def test_time_exit_gives_up_on_green_zombie_never_proven():
    """A green trade at the time-stop that never hit its give-up floor exits."""
    trade = _time_exit_trade("BUY", best_mfe_pct=0.005)
    candle = _candle(100.5, 101.0, 100.0, 100.5)  # currently green, but peaked +0.5%
    price, reason = _check_exit(
        candle, trade, bar_idx=13, min_hold_bars=0, dead_trade_flatten_bars=0,
    )
    assert reason == "time_exit"
    assert price == 100.5


def test_time_exit_keep_running_after_mfe_proof():
    """A green trade that once printed ≥ the give-up floor keeps running."""
    trade = _time_exit_trade("BUY", best_mfe_pct=0.03)  # proved itself at +3%
    candle = _candle(100.5, 101.0, 100.0, 100.5)
    price, reason = _check_exit(
        candle, trade, bar_idx=13, min_hold_bars=0, dead_trade_flatten_bars=0,
    )
    assert reason != "time_exit"
    assert price is None


def test_time_exit_give_up_applies_to_shorts():
    trade = _time_exit_trade("SELL", best_mfe_pct=0.005)
    candle = _candle(99.5, 100.0, 99.0, 99.5)  # short is green (close < entry)
    price, reason = _check_exit(
        candle, trade, bar_idx=13, min_hold_bars=0, dead_trade_flatten_bars=0,
    )
    assert reason == "time_exit"
    assert price == 99.5
