"""Persistent paper signal log file (JSONL) + scanner append."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path

from patterns.base_pattern import TradeSignal
from core.scanner import MarketScanner
from core import signal_log_store as sls


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
    )
    defaults.update(kw)
    return TradeSignal(**defaults)


class SignalLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._prev = sls._log_dir
        sls._log_dir = self._dir

    def tearDown(self) -> None:
        sls._log_dir = self._prev

    def _scanner(self) -> MarketScanner:
        scanner = MarketScanner.__new__(MarketScanner)
        scanner._market = "us"
        scanner._signal_log = deque(maxlen=1000)
        scanner._signal_log_lock = threading.Lock()
        return scanner

    def test_append_and_snapshot(self) -> None:
        scanner = self._scanner()
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
        self.assertEqual(snap[0]["market"], "us")

        path = sls.signal_log_path("us")
        self.assertTrue(path.exists())
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["symbol"], "MSFT")

    def test_file_survives_memory_clear_until_reset(self) -> None:
        scanner = self._scanner()
        scanner._append_signal_log(_sig(), status="accepted", reason="ok")
        scanner.clear_signal_log_memory()
        self.assertEqual(scanner.signal_log_snapshot(), [])
        loaded = sls.load_signal_log("us")
        self.assertEqual(len(loaded), 1)
        sls.reset_signal_log("us")
        self.assertEqual(sls.load_signal_log("us"), [])
        self.assertEqual(sls.signal_log_path("us").read_text(), "")

    def test_us_and_ph_are_separate_files(self) -> None:
        sls.append_signal_log("us", {"symbol": "AAPL", "status": "accepted"})
        sls.append_signal_log("ph", {"symbol": "BDO", "status": "rejected"})
        us = sls.load_signal_log("us")
        ph = sls.load_signal_log("ph")
        self.assertEqual(us[0]["symbol"], "AAPL")
        self.assertEqual(ph[0]["symbol"], "BDO")
        sls.reset_signal_log("ph")
        self.assertEqual(len(sls.load_signal_log("us")), 1)
        self.assertEqual(sls.load_signal_log("ph"), [])


if __name__ == "__main__":
    unittest.main()
