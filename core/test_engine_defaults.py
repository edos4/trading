"""Parity checks for core.engine_defaults — paper/scanner/backtest must share
the same entry gates and sizing caps."""

from core.engine_defaults import (
    ENGINE,
    backtest_kwargs,
    describe_cooldown_rejection,
    describe_min_share_price_rejection,
    describe_regime_rejection,
    passes_cooldown,
    passes_min_confidence,
    passes_min_share_price,
    passes_regime_filter,
    risk_gate_kwargs,
    seed_cooldown_from_trades,
    sizing_kwargs,
    structure_filters_enabled,
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
    assert ENGINE.max_position_pct == 0.10
    assert ENGINE.risk_per_trade_pct == 0.0075
    assert ENGINE.max_gross_exposure_pct == 1.0
    assert ENGINE.regime_hysteresis_pct == 0.015
    assert ENGINE.min_confidence == 0.65
    assert ENGINE.regime_filter is True
    assert ENGINE.pattern_only is False
    assert ENGINE.cooldown_bars == 20
    assert ENGINE.profit_take_pct is None
    assert ENGINE.profit_lock_frac == 0.25
    assert ENGINE.profit_lock_trigger_r == 1.0
    assert ENGINE.first_bar_invalidation_enabled is True
    assert ENGINE.dead_trade_flatten_bars == 3
    from config import DISABLED_PATTERNS
    # 002/004/008 posted 0-25% win rates whenever the regime filter was
    # bypassed (pattern_only paper runs) — disabled by default now.
    assert "pattern_002_double_top" in DISABLED_PATTERNS
    assert "pattern_008_head_and_shoulders" in DISABLED_PATTERNS
    assert "pattern_004_rounding_bottom" in DISABLED_PATTERNS
    assert "pattern_006_upward_channel" in DISABLED_PATTERNS
    # 007 off until first-bar invalidation proves out in paper A/B.
    assert "pattern_007_descending_channel" in DISABLED_PATTERNS
    # 010 pennant: "previously isolated as weak" — same bucket as 009.
    # Added 2026-09-02 so the list can't silently drift like 002/004/006/008 did.
    assert "pattern_010_pennant" in DISABLED_PATTERNS

    assert passes_min_confidence(_sig(confidence=0.65))
    assert not passes_min_confidence(_sig(confidence=0.64))

    assert structure_filters_enabled(False)
    assert structure_filters_enabled(None)
    assert not structure_filters_enabled(True)

    assert passes_min_share_price(_sig(price=5.0), min_share_price=5.0)
    assert not passes_min_share_price(_sig(price=4.99), min_share_price=5.0)
    price_reason = describe_min_share_price_rejection(_sig(price=2.0), 5.0, market="us")
    assert price_reason is not None and "Min share-price gate" in price_reason
    assert "$2.00" in price_reason
    ph_price = describe_min_share_price_rejection(_sig(price=2.0), 5.0, market="ph")
    assert "₱2.00" in ph_price
    assert passes_min_share_price(_sig(price=2.0), min_share_price=None)

    # Short history → regime no-op (same as backtester).
    assert passes_regime_filter(_sig(), _Store())

    # 200+ bars: BUY well below SMA200 rejected.
    below = [100.0] * 200 + [50.0]
    assert not passes_regime_filter(_sig(action="BUY"), _Store(below))
    buy_reason = describe_regime_rejection(
        _sig(action="BUY"), _Store(below), market="us",
    )
    assert buy_reason is not None
    assert "SMA200 regime filter" in buy_reason
    assert "counter-trend BUY blocked" in buy_reason
    assert "$50.00" in buy_reason
    ph_reason = describe_regime_rejection(
        _sig(action="BUY"), _Store(below), market="ph",
    )
    assert "₱50.00" in ph_reason
    assert "$" not in ph_reason
    # SELL below SMA200 allowed.
    assert passes_regime_filter(_sig(action="SELL"), _Store(below))
    assert describe_regime_rejection(_sig(action="SELL"), _Store(below)) is None

    # Above SMA200: SELL blocked, BUY allowed.
    above = [50.0] * 200 + [100.0]
    sell_reason = describe_regime_rejection(_sig(action="SELL"), _Store(above))
    assert sell_reason is not None
    assert "counter-trend SELL blocked" in sell_reason
    assert describe_regime_rejection(_sig(action="BUY"), _Store(above)) is None

    # Channel patterns used to skip SMA200; they no longer do.
    assert describe_regime_rejection(
        _sig(pattern="pattern_007_descending_channel", action="BUY"),
        _Store(below),
    ) is not None
    assert describe_regime_rejection(
        _sig(pattern="pattern_006_upward_channel", action="SELL"),
        _Store(above),
    ) is not None
    assert ENGINE.regime_exempt_patterns == ()
    assert ENGINE.breakeven_trigger_pct == 0.06
    assert ENGINE.profit_take_pct is None
    assert ENGINE.profit_lock_frac == 0.25
    assert ENGINE.profit_lock_trigger_r == 1.0

    # 1.5% hysteresis: ~1% the wrong side of SMA200 is a near-miss, not a block.
    buy_near = [100.0] * 200 + [99.0]
    assert passes_regime_filter(_sig(action="BUY"), _Store(buy_near))
    sell_near = [50.0] * 200 + [50.6]
    assert passes_regime_filter(_sig(action="SELL"), _Store(sell_near))

    tracker = {("TEST", "pattern_003_double_bottom"): (0, True)}
    assert not passes_cooldown(_sig(), bar_idx=5, cooldown_tracker=tracker)
    cool = describe_cooldown_rejection(_sig(), 5, tracker)
    assert cool is not None and "Post-loss cooldown" in cool
    assert "chopping the same name" in cool
    assert passes_cooldown(_sig(), bar_idx=20, cooldown_tracker=tracker)

    # Loss on 007 still blocks a later 003 on the same symbol.
    other = {("TEST", "pattern_007_descending_channel"): (0, True)}
    assert not passes_cooldown(_sig(), bar_idx=5, cooldown_tracker=other)
    assert passes_cooldown(
        _sig(symbol="OTHER"), bar_idx=5, cooldown_tracker=other,
    )
    seeded: dict[tuple[str, str], tuple[int, bool]] = {}
    seed_cooldown_from_trades(
        seeded,
        [
            _sig(),  # not a trade — missing exit_bar_idx, ignored
            type("T", (), {
                "symbol": "FOXO", "pattern": "pattern_007_descending_channel",
                "exit_bar_idx": 3, "pnl": -50.0,
            })(),
            type("T", (), {
                "symbol": "FOXO", "pattern": "pattern_007_descending_channel",
                "exit_bar_idx": 12, "pnl": 10.0,
            })(),
        ],
    )
    assert seeded[("FOXO", "pattern_007_descending_channel")] == (12, False)
    assert passes_cooldown(
        _sig(symbol="FOXO", pattern="pattern_003_double_bottom"),
        bar_idx=15, cooldown_tracker=seeded,
    )

    rg = risk_gate_kwargs()
    assert rg["hard_stop_percentage"] == 0.12
    assert "max_position_pct" not in rg

    sk = sizing_kwargs(account_value=50_000.0)
    assert sk["max_position_pct"] == 0.10
    assert sk["risk_per_trade_pct"] == 0.0075
    assert sk["account_value"] == 50_000.0

    bt = backtest_kwargs(pattern_filter="double_bottom", market="us")
    assert bt["min_confidence"] == 0.65
    assert bt["max_position_pct"] == 0.10
    assert bt["max_gross_exposure_pct"] == 1.0
    assert bt["breakeven_trigger_pct"] == 0.06
    assert bt["profit_take_pct"] is None
    assert bt["profit_lock_frac"] == 0.25
    assert bt["profit_lock_trigger_r"] == 1.0
    assert bt["breakeven_buffer_pct"] == 0.0015
    assert bt["min_share_price"] == 10.0
    assert "regime_hysteresis_pct" not in bt
    assert "regime_exempt_patterns" not in bt
    assert bt["pattern_filter"] == "double_bottom"
    assert bt["market"] == "us"
    assert bt["long_only"] is False
    assert bt["pattern_only"] is False

    print("engine_defaults: all checks passed")


def test_engine_defaults_parity():
    demo()


def test_all_short_patterns_regime_required():
    from core.engine_defaults import REGIME_REQUIRED_PATTERNS

    for pat in (
        "pattern_002_double_top",
        "pattern_005_rounding_top",
        "pattern_006_upward_channel",
        "pattern_008_head_and_shoulders",
    ):
        assert pat in REGIME_REQUIRED_PATTERNS


def test_double_bottom_regime_required():
    """2026-09-02 review: 003 went 0-for-3 Pattern-only (no SMA200 gate)
    after the same book's documented 57%/2.60pf history for 003 came from
    a run where the regime gate WAS on. 003 must clear SMA200 even in
    Pattern-only, same as the other reversal patterns."""
    from core.engine_defaults import REGIME_REQUIRED_PATTERNS

    assert "pattern_003_double_bottom" in REGIME_REQUIRED_PATTERNS


def test_dead_trade_threshold_half_percent():
    assert ENGINE.dead_trade_mfe_threshold_pct == 0.005


if __name__ == "__main__":
    demo()
