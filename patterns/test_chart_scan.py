"""Chart explorer must look at ~20 recently formed bars, not only today."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from data.tv_client import OHLCVCandle
from patterns.base_pattern import FORMATION_BARS, BasePattern, TradeSignal
from patterns.chart_scan import latest_signals_over_lookback


class _TriggerOnBar(BasePattern):
    """Fires only when the last stored bar index equals `fire_at`."""

    def __init__(self, fire_at: int):
        self._fire_at = fire_at

    @property
    def name(self) -> str:
        return "pattern_test_trigger"

    @property
    def timeframes(self) -> list[str]:
        return ["1d"]

    def analyze(self, snapshot, store):
        df = store.get_df(snapshot.symbol, snapshot.timeframe, min_bars=2)
        if df is None:
            return None
        if len(df) - 1 != self._fire_at:
            return None
        return TradeSignal(
            symbol=snapshot.symbol,
            action="BUY",
            pattern=self.name,
            timeframe=snapshot.timeframe,
            confidence=0.8,
            price=float(df["close"].iloc[-1]),
            qty=1,
            notes="exact-bar trigger",
        )


def _candles(n: int) -> list[OHLCVCandle]:
    return [
        OHLCVCandle(
            open=10.0, high=11.0, low=9.0, close=10.0 + i * 0.01,
            volume=1_000.0,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(n)
    ]


class ChartScanLookbackTests(unittest.TestCase):
    def test_lookback_finds_trigger_several_days_ago(self):
        n = 50
        pattern = _TriggerOnBar(fire_at=n - 1 - 5)
        signals = latest_signals_over_lookback(
            [pattern], "TEST", "1d", _candles(n),
            session_tz="America/New_York",
        )
        self.assertEqual(len(signals), 1)
        self.assertIn("5 bar", signals[0].notes)

    def test_lookback_is_twenty_days(self):
        self.assertEqual(FORMATION_BARS, 20)


if __name__ == "__main__":
    unittest.main()
