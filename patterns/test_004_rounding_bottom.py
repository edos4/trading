"""Rounding-bottom formation: wait ~20 days, not the day-1 bounce."""

from __future__ import annotations

import importlib
import unittest

import pandas as pd

from analysis.indicator_engine import IndicatorEngine

RoundingBottomPattern = importlib.import_module(
    "patterns.004_rounding_bottom"
).RoundingBottomPattern


class RoundingBottomFormationTests(unittest.TestCase):
    def test_entry_trigger_skips_day1_recovery(self):
        p = RoundingBottomPattern()
        n = 55
        close = [100.0 - i * 0.5 for i in range(20)] + [90.0 + i * 0.4 for i in range(35)]
        high = [c + 1 for c in close]
        low = [c - 1 for c in close]
        rsi = pd.Series([40.0 + i * 0.2 for i in range(n)])
        df = pd.DataFrame({
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": [1_000_000.0] * n,
        })
        ind = IndicatorEngine(df)
        bottom_idx = 19
        early = p._find_entry_trigger(ind, rsi, bottom_idx, cur=bottom_idx + 5)
        self.assertIsNone(early)
        late = p._find_entry_trigger(ind, rsi, bottom_idx, cur=n - 1)
        self.assertIsNotNone(late)
        self.assertGreaterEqual(late - bottom_idx, p.ENTRY_MIN_BARS_AFTER_BOTTOM)

    def test_gap_min_matches_formation_bars(self):
        p = RoundingBottomPattern()
        self.assertEqual(p.ENTRY_MIN_BARS_AFTER_BOTTOM, 20)
        self.assertEqual(p.RECOVERY_MIN, 0.60)
        self.assertEqual(p.MIN_UPSIDE, 0.05)


if __name__ == "__main__":
    unittest.main()
