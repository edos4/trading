"""Double-bottom entry/exit geometry after the 2026-08-17 paper review."""

import importlib

DoubleBottomPattern = importlib.import_module(
    "patterns.003_double_bottom"
).DoubleBottomPattern


def test_exit_levels_use_l2_stop_and_entry_target():
    p = DoubleBottomPattern()
    # Neckline-break entry at 100; L1=85 (15% W); L2=90.
    stop, target = p._exit_levels(close=100.0, neckline=100.0, l1_low=85.0, l2_low=90.0)
    assert stop == round(90.0 * p.STOP_BELOW_L2, 4)
    assert stop < 100.0
    # Measured move 15% beats the 12% floor.
    assert target == 115.0


def test_exit_levels_floor_is_twelve_percent():
    p = DoubleBottomPattern()
    # Shallow W: measured move 7% would fail min R:R after a 6% hard cap.
    stop, target = p._exit_levels(close=100.0, neckline=100.0, l1_low=93.0, l2_low=94.0)
    assert target == 112.0
    assert stop < 100.0


def test_neckline_break_requires_buffer_above_peak():
    import pandas as pd

    p = DoubleBottomPattern()
    close = pd.Series([90.0] * 30)
    close.iloc[25] = 100.01  # barely above neckline 100
    assert p._neckline_break_idx(close, l2_idx=20, cur=25, neckline=100.0) is None
    close.iloc[26] = 100.6  # 0.6% above neckline
    assert p._neckline_break_idx(close, l2_idx=20, cur=26, neckline=100.0) == 26


def test_w_requires_about_one_month_to_form():
    p = DoubleBottomPattern()
    assert p.L1_L2_GAP_MIN == 21


def test_no_day7_entry_without_neckline_break():
    """Unconfirmed W (the 79-fill paper book) must not produce a setup."""
    import pandas as pd
    from analysis.indicator_engine import IndicatorEngine

    p = DoubleBottomPattern()
    n = 40
    close = [100.0] * n
    high = [101.0] * n
    low = [99.0] * n
    # L1 at 10, peak at 18, L2 at 25 — never closes above the peak.
    close[10] = 80.0
    low[10] = 79.0
    high[10] = 81.0
    close[18] = 95.0
    high[18] = 96.0
    low[18] = 90.0
    close[25] = 82.0
    low[25] = 81.0
    high[25] = 83.0
    df = pd.DataFrame({
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": [1_000_000.0] * n,
    })
    ind = IndicatorEngine(df)
    rsi = pd.Series([50.0] * n)
    rsi.iloc[10] = 25.0
    rsi.iloc[25] = 42.0
    setup = p._evaluate_pair(ind, rsi, 10, 25, cur=n - 1)
    assert setup is None
