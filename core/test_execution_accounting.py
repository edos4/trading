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


def test_long_multiple_stops_fill_worst_plausible():
    trade = _trade("BUY", 100, 95, None)
    trade.breakeven_trigger_pct = 0.0
    trade._best_pnl_pct = 0.0
    # Open above both levels, low crossing both the breakeven floor (100.15)
    # and the hard stop (95) in one bar. Worst plausible protective fill is 95.
    price, reason = _check_exit(_candle(102, 103, 93, 94), trade, 2)
    assert reason == "stop_loss"
    assert price == 95


def test_short_multiple_stops_fill_worst_plausible():
    trade = _trade("SELL", 100, 105, None)
    trade.breakeven_trigger_pct = 0.0
    trade._best_pnl_pct = 0.0
    # Open below both levels, high crossing both the breakeven floor (99.85)
    # and the hard stop (105) in one bar. Worst plausible protective fill is 105.
    price, reason = _check_exit(_candle(98, 107, 97, 106), trade, 2)
    assert reason == "stop_loss"
    assert price == 105


def test_deferred_neckline_time_stop_starts_on_signal_bar():
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
    position = _open_trade(signal, fill, bar_idx=11)
    assert position.entry_bar_idx == 11
    assert position.neckline_break_bar_idx == 10

    # Three bars after the signal bar is the time-stop, even though the fill
    # itself occurred on the following bar.
    later = _candle(102, 103, 101, 102)
    price, reason = _check_exit(later, position, bar_idx=13, min_hold_bars=0)
    assert reason == "time_exit"
    assert price == 102
