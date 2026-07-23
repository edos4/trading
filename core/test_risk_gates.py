"""Smallest possible check that apply_risk_gates still does its job:
hard-stop caps a missing/too-wide stop, and a thin R:R gets rejected. Also
checks days_held() reports simulated (bar-time) duration for closed trades,
not the real wall-clock minutes a stream replay took to run."""

from datetime import datetime, timedelta, timezone

from core.backtester import BacktestTrade, apply_risk_gates
from core.paper_trader import days_held
from patterns.base_pattern import TradeSignal


class _NoDataStore:
    """Stands in for OHLCVStore — returns no df, so the ATR-dependent
    gates (min_atr_stop_multiple, atr_stop_floor_multiple) no-op and only
    the ATR-independent gates (hard_stop_percentage, min_reward_risk_ratio,
    trailing_activation_default) are exercised."""

    def get_df(self, symbol, timeframe, min_bars=1):
        return None


def _signal(**kw) -> TradeSignal:
    base = dict(
        symbol="TEST", timeframe="1d", pattern="pattern_003_double_bottom",
        action="BUY", price=100.0, confidence=0.9, qty=1,
        stop_loss=None, take_profit=None, trailing_stop_pct=None,
    )
    base.update(kw)
    return TradeSignal(**base)


def demo():
    store = _NoDataStore()

    # No stop_loss at all -> hard_stop_percentage must fill one in.
    sig = _signal()
    assert apply_risk_gates(sig, store, "TEST", "1d", hard_stop_percentage=0.06)
    assert sig.stop_loss == 94.0, sig.stop_loss

    # Structural stop wider than the hard cap -> capped, never loosened past it.
    sig = _signal(stop_loss=80.0)
    assert apply_risk_gates(sig, store, "TEST", "1d", hard_stop_percentage=0.06)
    assert sig.stop_loss == 94.0, sig.stop_loss

    # Structural stop tighter than the hard cap -> left alone.
    sig = _signal(stop_loss=98.0)
    assert apply_risk_gates(sig, store, "TEST", "1d", hard_stop_percentage=0.06)
    assert sig.stop_loss == 98.0, sig.stop_loss

    # Reward:risk below the minimum -> signal dropped.
    sig = _signal(stop_loss=95.0, take_profit=102.0)  # R:R = 2/5 = 0.4
    assert not apply_risk_gates(sig, store, "TEST", "1d", min_reward_risk_ratio=1.5)

    # Reward:risk at/above the minimum -> signal kept.
    sig = _signal(stop_loss=95.0, take_profit=110.0)  # R:R = 10/5 = 2.0
    assert apply_risk_gates(sig, store, "TEST", "1d", min_reward_risk_ratio=1.5)

    # trailing_activation_default only fills in when trailing_stop_pct is set.
    sig = _signal(trailing_stop_pct=0.05)
    apply_risk_gates(sig, store, "TEST", "1d", trailing_activation_default=0.02)
    assert sig.trailing_activation_pct == 0.02, sig.trailing_activation_pct

    print("apply_risk_gates: all checks passed")

    # A stream replay: bars are days apart in sim time but the fills all
    # happen within the same real minute. days_held must report the sim gap.
    sim_start = datetime(2024, 3, 1, tzinfo=timezone.utc)
    wall_start = datetime.now(timezone.utc)
    trade = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="p", action="BUY",
        entry_date=wall_start, exit_date=wall_start + timedelta(seconds=5),
        entry_price=100.0, exit_price=105.0, pnl=5.0, pnl_pct=5.0,
        sim_entry_date=sim_start, sim_exit_date=sim_start + timedelta(days=3),
    )
    assert days_held(trade) == 3.0, days_held(trade)

    # No sim dates (e.g. an account.json saved before this fix) -> falls
    # back to wall-clock entry/exit, same as before.
    trade_no_sim = BacktestTrade(
        symbol="TEST", timeframe="1d", pattern="p", action="BUY",
        entry_date=wall_start, exit_date=wall_start + timedelta(seconds=5),
        entry_price=100.0, exit_price=105.0, pnl=5.0, pnl_pct=5.0,
    )
    assert abs(days_held(trade_no_sim) - 5 / 86400) < 1e-9

    print("days_held: all checks passed")


if __name__ == "__main__":
    demo()
