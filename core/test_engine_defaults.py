"""Parity checks for core.engine_defaults — paper/scanner/backtest must share
the same entry gates and sizing caps."""

from core.engine_defaults import (
    ENGINE,
    backtest_kwargs,
    describe_cooldown_rejection,
    describe_regime_rejection,
    passes_cooldown,
    passes_min_confidence,
    passes_regime_filter,
    risk_gate_kwargs,
    sizing_kwargs,
)
from patterns.base_pattern import TradeSignal


def _sig(**kw) -> TradeSignal:
    base = dict(
        symbol="TEST", timeframe="1d", pattern="pattern_003_double_bottom",
        action="BUY", price=100.0, confidence=0.9, qty=1,
    )
    base.update(kw)
    return TradeSignal(**base)


class _Store:
    def __init__(self, closes: list[float] | None = None):
        self.closes = closes

    def get_df(self, symbol, timeframe, min_bars=1):
        if self.closes is None:
            return None
        import pandas as pd

        return pd.DataFrame({"close": self.closes})


def demo():
    assert ENGINE.max_position_pct == 0.33
    assert ENGINE.min_confidence == 0.6
    assert ENGINE.regime_filter is True
    assert ENGINE.cooldown_bars == 10

    assert passes_min_confidence(_sig(confidence=0.6))
    assert not passes_min_confidence(_sig(confidence=0.59))

    # Short history → regime no-op (same as backtester).
    assert passes_regime_filter(_sig(), _Store())

    # 200+ bars: BUY below SMA200 rejected.
    below = [100.0] * 200 + [50.0]
    assert not passes_regime_filter(_sig(action="BUY"), _Store(below))
    buy_reason = describe_regime_rejection(_sig(action="BUY"), _Store(below))
    assert buy_reason is not None
    assert "SMA200 regime filter" in buy_reason
    assert "counter-trend BUY blocked" in buy_reason
    # SELL below SMA200 allowed.
    assert passes_regime_filter(_sig(action="SELL"), _Store(below))
    assert describe_regime_rejection(_sig(action="SELL"), _Store(below)) is None

    # Above SMA200: SELL blocked, BUY allowed.
    above = [50.0] * 200 + [100.0]
    sell_reason = describe_regime_rejection(_sig(action="SELL"), _Store(above))
    assert sell_reason is not None
    assert "counter-trend SELL blocked" in sell_reason
    assert describe_regime_rejection(_sig(action="BUY"), _Store(above)) is None

    tracker = {("TEST", "pattern_003_double_bottom"): (0, True)}
    assert not passes_cooldown(_sig(), bar_idx=5, cooldown_tracker=tracker)
    cool = describe_cooldown_rejection(_sig(), 5, tracker)
    assert cool is not None and "Post-loss cooldown" in cool
    assert passes_cooldown(_sig(), bar_idx=10, cooldown_tracker=tracker)

    rg = risk_gate_kwargs()
    assert rg["hard_stop_percentage"] == 0.06
    assert "max_position_pct" not in rg

    sk = sizing_kwargs(account_value=50_000.0)
    assert sk["max_position_pct"] == 0.33
    assert sk["risk_per_trade_pct"] == 0.02
    assert sk["account_value"] == 50_000.0

    bt = backtest_kwargs(pattern_filter="double_bottom")
    assert bt["min_confidence"] == 0.6
    assert bt["max_position_pct"] == 0.33
    assert bt["pattern_filter"] == "double_bottom"

    print("engine_defaults: all checks passed")


if __name__ == "__main__":
    demo()
