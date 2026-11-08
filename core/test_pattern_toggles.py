"""Settings-tab pattern toggles: catalog, shipped-off set and runtime apply."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import config
from config import DISABLED_PATTERNS, disabled_pattern_names, disabled_patterns_v2_names
from core import pattern_toggles
from core.pattern_jobs import detect_v2_batch, make_v2_pool
from patterns_v2 import loader as patterns_v2_loader
from patterns_v2.signals import signals_from_raw

V1 = pattern_toggles.FIELD_V1
V2 = pattern_toggles.FIELD_V2


def _double_bottom_bars() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The textbook W from patterns_v2/test_double_bottom.py (80 bars)."""
    xs, ys = [0, 20, 35, 50, 65, 79], [100, 140, 90, 110, 91, 115]
    close = np.interp(np.arange(xs[-1] + 1), xs, ys)
    return close + 1, close - 1, close


class PatternToggleConfigTests(unittest.TestCase):
    def test_lists_are_empty_by_default_and_parse_comma_separated_names(self):
        self.assertEqual(DISABLED_PATTERNS, [])
        with patch.object(config.settings, "disabled_patterns", "a, b ,,c"):
            self.assertIn("b", disabled_pattern_names())
            self.assertEqual(disabled_pattern_names(), sorted(set(DISABLED_PATTERNS) | {"a", "b", "c"}))
        with patch.object(config.settings, "disabled_patterns_v2", " ,double_bottom, "):
            self.assertEqual(disabled_patterns_v2_names(), ["double_bottom"])

    def test_shipped_mute_list_is_always_honoured(self):
        with patch.object(config, "DISABLED_PATTERNS", ["pattern_003_double_bottom"]):
            with patch.object(config.settings, "disabled_patterns", ""):
                self.assertIn("pattern_003_double_bottom", disabled_pattern_names())


class PatternToggleCatalogTests(unittest.TestCase):
    def test_v1_catalog_covers_every_shipped_detector(self):
        entries = {entry["id"]: entry for entry in pattern_toggles.catalog(V1)}
        self.assertEqual(len(entries), 10)
        self.assertIn("pattern_003_double_bottom", entries)
        self.assertIn("pattern_011_breakout_retest", entries)
        self.assertFalse(entries["pattern_003_double_bottom"]["forced"])
        # 011 is retired via `skipped = True`, so its box can never be ticked.
        self.assertTrue(entries["pattern_011_breakout_retest"]["forced"])
        self.assertEqual(entries["pattern_011_breakout_retest"]["reason"], "Retired")
        self.assertTrue(all(entry["label"] for entry in entries.values()))

    def test_v2_catalog_is_every_name_the_package_can_emit(self):
        module = patterns_v2_loader.detector_module()
        entries = {entry["id"]: entry for entry in pattern_toggles.catalog(V2)}
        self.assertEqual(set(entries), set(module.PATTERN_NAMES) | set(module.DISABLED))
        self.assertEqual(len(entries), len(module.PATTERN_NAMES) + len(module.DISABLED))
        self.assertFalse(entries["double_bottom"]["forced"])
        # The package's own default-muted names stay off whichever box is clicked.
        self.assertTrue(entries["head_shoulders"]["forced"])
        self.assertIn("head_shoulders", pattern_toggles.forced(V2))

    def test_muted_lists_the_shipped_off_names_even_with_empty_settings(self):
        with patch.object(config.settings, "disabled_patterns", ""), \
             patch.object(config.settings, "disabled_patterns_v2", ""):
            self.assertTrue(set(pattern_toggles.forced(V1)) <= set(pattern_toggles.muted(V1)))
            self.assertEqual(set(pattern_toggles.muted(V2)),
                             set(pattern_toggles.forced(V2)))


class PatternToggleApplyTests(unittest.TestCase):
    def test_apply_mutes_configured_names_without_dropping_the_shipped_set(self):
        module = patterns_v2_loader.detector_module()
        pristine = module.DISABLED
        try:
            with patch.object(config.settings, "disabled_patterns_v2", "double_bottom"), \
                 patch.object(config.settings, "disabled_patterns", "pattern_003_double_bottom"):
                muted = pattern_toggles.apply()
                self.assertEqual(set(module.DISABLED), set(muted[V2]))
                self.assertIn("double_bottom", module.DISABLED)
                self.assertIn("head_shoulders", module.DISABLED)
                self.assertNotIn("bull_flag", module.DISABLED)
                self.assertIn("pattern_003_double_bottom", muted[V1])
        finally:
            setattr(module, "DISABLED", pristine)
        # A second apply must not turn the first one's names into permanent forced ones.
        self.assertEqual(set(pattern_toggles.forced(V2)), set(pristine))

    def test_the_detector_really_skips_a_muted_v2_pattern(self):
        high, low, close = _double_bottom_bars()
        detect = patterns_v2_loader.detect_fn()
        module = patterns_v2_loader.detector_module()
        pristine = module.DISABLED
        try:
            self.assertIn("double_bottom", {s["pattern"] for s in detect(high, low, close)})
            with patch.object(config.settings, "disabled_patterns_v2", "double_bottom"):
                pattern_toggles.apply()
                found = {s["pattern"] for s in detect(high, low, close)}
            self.assertNotIn("double_bottom", found)
        finally:
            setattr(module, "DISABLED", pristine)


class PatternTogglePaperScanTests(unittest.TestCase):
    """A paper book scans patternsv2 in spawned workers, not in this process."""

    def test_the_worker_pool_inherits_the_settings_mute(self):
        high, low, close = _double_bottom_bars()
        module = patterns_v2_loader.detector_module()
        pristine = module.DISABLED
        try:
            with patch.object(config.settings, "disabled_patterns_v2", "double_bottom"):
                muted = pattern_toggles.muted(V2)
            pool = make_v2_pool(2, disabled=muted)
            self.assertIsNotNone(pool)
            try:
                found = pool.submit(
                    detect_v2_batch, [("TEST", "1d", high, low, close)],
                ).result(timeout=120)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            self.assertNotIn("double_bottom", {s["pattern"] for s in found[0]})
        finally:
            setattr(module, "DISABLED", pristine)

    def test_the_scanner_hands_the_mute_list_to_the_v2_pool(self):
        from core import pattern_jobs as jobs
        from core.scanner import MarketScanner

        scanner = MarketScanner.__new__(MarketScanner)
        scanner._indicators_only = False
        scanner._patterns_v2 = True
        scanner._analyze_pool = None
        calls = []
        with patch.object(
            jobs, "make_v2_pool",
            lambda workers, disabled=(): calls.append(set(disabled)),
        ), patch.object(config.settings, "disabled_patterns_v2", "double_bottom"):
            scanner._open_analyze_pool()
        self.assertIn("double_bottom", calls[0])

    def test_the_converter_drops_a_pattern_muted_after_detection(self):
        """A pool opened before the save must not emit the newly muted name."""
        high, low, close = _double_bottom_bars()
        found = patterns_v2_loader.detect_fn()(high, low, close)
        module = patterns_v2_loader.detector_module()
        pristine = module.DISABLED
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close,
             "volume": np.full(len(close), 1000.0)},
            index=pd.bdate_range("2024-01-02", periods=len(close)),
        )

        def names() -> set[str]:
            return {
                signal.pattern
                for signal in signals_from_raw(df, found, symbol="TEST", timeframe="1d")
            }

        try:
            with patch.object(config.settings, "disabled_patterns_v2", ""):
                self.assertIn("double_bottom", names())
            with patch.object(config.settings, "disabled_patterns_v2", "double_bottom"):
                pattern_toggles.apply()
                self.assertNotIn("double_bottom", names())
        finally:
            setattr(module, "DISABLED", pristine)


if __name__ == "__main__":
    unittest.main()
