from __future__ import annotations

from datetime import datetime, timezone

from core.backtester import BacktestTrade
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
