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
    trade = _trade("BUY", 100, 90, 120, qty=10)
    trade.exit_price = 120
    trade.pnl = 20.0 - (100.0 + 120.0) * 0.001
    accepted, rejected = _apply_capital_ledger(
        [trade], 100_000, 0.01, "notional", 0.02,
        max_gross_exposure_pct=1.0, txn_cost_pct=0.001,
    )
    assert rejected == 0
    assert accepted == [trade]
    # 2% notional => 20 shares; realized capital includes both entry and exit fees.
    expected = 100_000 + (120 - 100) * 20 - (100 + 120) * 20 * 0.001
    actual = 100_000 + trade.pnl * trade.qty
    assert abs(actual - expected) < 1e-9
