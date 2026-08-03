"""Unit tests for MarketScanner signal log ring buffer."""

from __future__ import annotations

import unittest
from collections import deque
import threading

from patterns.base_pattern import TradeSignal
from core.scanner import MarketScanner


def _sig(**kw) -> TradeSignal:
    defaults = dict(
        symbol="AAPL",
        timeframe="1d",
        action="BUY",
        pattern="demo_pattern",
        confidence=0.8,
        price=100.0,
        qty=1,
        stop_loss=95.0,
        take_profit=110.0,
        trailing_stop_pct=None,
        notes="",
    )
    defaults.update(kw)
    return TradeSignal(**defaults)


class SignalLogTests(unittest.TestCase):
    def test_append_and_snapshot(self) -> None:
        scanner = MarketScanner.__new__(MarketScanner)
        scanner._signal_log = deque(maxlen=1000)
        scanner._signal_log_lock = threading.Lock()

        ok = _sig()
        bad = _sig(symbol="MSFT", confidence=0.1)
        scanner._append_signal_log(ok, status="accepted", reason="queued for next-bar fill")
        scanner._append_signal_log(
            bad, status="rejected", reason="confidence 0.10 < min 0.60",
        )
        snap = scanner.signal_log_snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap[0]["status"], "accepted")
        self.assertEqual(snap[1]["status"], "rejected")
        self.assertIn("confidence", snap[1]["reason"])
        self.assertEqual(snap[1]["symbol"], "MSFT")


if __name__ == "__main__":
    unittest.main()
