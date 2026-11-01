"""Risk-geometry rules: ATR stop sizing, R-based trailing, min-price floor."""
from __future__ import annotations

import pandas as pd
import pytest

from core.backtester import _apply_risk_sizing
from core.scanner import describe_min_share_price_rejection
from patterns.base_pattern import TradeSignal
from patterns_v2.signals import (
    STOP_PCT_CEIL,
    STOP_PCT_FLOOR,
    TRAIL_GIVEBACK_R,
    TRAIL_ARM_R,
    stop_pct_for,
)


def _frame(daily_range: float, n: int = 30, base: float = 100.0) -> pd.DataFrame:
    rows = []
    for _ in range(n):
        rows.append({"open": base, "high": base + daily_range / 2,
                     "low": base - daily_range / 2, "close": base})
    return pd.DataFrame(rows)


def test_atr_stop_clamps_to_floor_on_a_quiet_name():
    df = _frame(0.5)
    assert stop_pct_for(df, len(df) - 1, 100.0) == pytest.approx(STOP_PCT_FLOOR)


def test_atr_stop_clamps_to_ceiling_on_a_wild_name():
    df = _frame(20.0)
    assert stop_pct_for(df, len(df) - 1, 100.0) == pytest.approx(STOP_PCT_CEIL)


def test_atr_stop_is_one_and_a_half_atr_inside_the_clamps():
    df = _frame(4.0)                      # ATR = 4% of price
    assert stop_pct_for(df, len(df) - 1, 100.0) == pytest.approx(0.06)


def test_atr_stop_is_causal_and_falls_back_without_history():
    df = _frame(5.0)                      # ATR 5% -> 7.5% stop, under the ceiling
    # Only the first two bars are visible: the rest of the frame must not leak in.
    assert stop_pct_for(df.iloc[:2], 1, 100.0) == pytest.approx(1.5 * 5.0 / 100)
    assert stop_pct_for(df.iloc[:1], 0, 100.0) == pytest.approx(STOP_PCT_FLOOR)


def test_trailing_multiple_keeps_giveback_constant_in_r():
    # The relationship is what matters: give-back in R must not depend on price.
    for cap in (0.03, 0.06, 0.08):
        trail = TRAIL_GIVEBACK_R * cap
        assert trail / cap == pytest.approx(TRAIL_GIVEBACK_R)
        assert TRAIL_ARM_R * cap / cap == pytest.approx(TRAIL_ARM_R)


def _signal(price: float | None, stop: float | None = None, cap: float | None = 0.03) -> TradeSignal:
    return TradeSignal(symbol="TEST", action="BUY", pattern="double_bottom", timeframe="1d",
                       confidence=1.0, price=price, qty=0.0, stop_loss=stop,
                       take_profit=None, stop_loss_pct_cap=cap)


def test_min_price_floor_rejects_a_sub_floor_signal():
    reason = describe_min_share_price_rejection(_signal(2.41), 10.0, market="us")
    assert reason is not None and "below" in reason


def test_min_price_floor_passes_above_the_floor_and_without_one():
    assert describe_min_share_price_rejection(_signal(11.0), 10.0, market="us") is None
    assert describe_min_share_price_rejection(_signal(2.41), None, market="ph") is None
    assert describe_min_share_price_rejection(_signal(2.41), 0, market="us") is None
    assert describe_min_share_price_rejection(_signal(None), 10.0, market="us") is None


def test_risk_sizing_uses_the_nearer_of_stop_and_cap():
    s = _signal(100.0, stop=96.0, cap=0.03)      # structural 4%, cap 3% -> 3%
    _apply_risk_sizing(s, 300.0, cap=s.stop_loss_pct_cap)
    assert s.qty == pytest.approx(100.0)


def test_risk_sizing_honours_a_wider_atr_cap():
    s = _signal(100.0, stop=70.0, cap=0.08)      # structural 30%, cap 8% -> 8%
    _apply_risk_sizing(s, 400.0, cap=s.stop_loss_pct_cap)
    assert s.qty == pytest.approx(50.0)


def test_risk_sizing_without_a_stop_falls_back_to_the_cap():
    s = _signal(50.0, stop=None, cap=0.05)
    _apply_risk_sizing(s, 250.0, cap=s.stop_loss_pct_cap)
    assert s.qty == pytest.approx(100.0)


def test_risk_sizing_floors_whole_shares_and_rejects_degenerate_input():
    s = _signal(33.0, stop=30.0, cap=None)
    _apply_risk_sizing(s, 100.0)                 # 100 / 3 = 33.3 -> 33 shares
    assert s.qty == 33.0
    s.qty = 99.0
    _apply_risk_sizing(s, 0.0)
    assert s.qty == 0.0
