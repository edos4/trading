"""Explorer pattern discovery must honor DISABLED_PATTERNS."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from web.services import discover_patterns


class DiscoverPatternsDisabledTests(unittest.TestCase):
    def test_excludes_disabled_pattern_names(self):
        disabled = ["pattern_012_ml_signal", "pattern_011_breakout_retest"]
        with patch("web.services.DISABLED_PATTERNS", disabled):
            names = {p.name for p in discover_patterns()}
        for name in disabled:
            self.assertNotIn(name, names)

    def test_explicit_disabled_list_overrides_config(self):
        all_names = {p.name for p in discover_patterns(disabled_patterns=[])}
        self.assertTrue(all_names)
        subset = {p.name for p in discover_patterns(disabled_patterns=list(all_names))}
        self.assertEqual(subset, set())


if __name__ == "__main__":
    unittest.main()
