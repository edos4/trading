"""Explorer pattern discovery must honor the Settings-tab pattern toggles."""

from __future__ import annotations

import unittest
import pytest

pytestmark = pytest.mark.usefixtures("published_pattern_catalog")
from unittest.mock import patch

from web.services import discover_patterns


class DiscoverPatternsDisabledTests(unittest.TestCase):
    def test_excludes_disabled_pattern_names(self):
        # 003/006 run by default, so an explicit toggle list must remove them.
        disabled = ["pattern_003_double_bottom", "pattern_006_upward_channel"]
        with patch("web.services.disabled_pattern_names", return_value=disabled):
            names = {p.name for p in discover_patterns()}
        for name in disabled:
            self.assertNotIn(name, names)

    def test_default_run_covers_ported_patterns_but_not_retired_011(self):
        # Exact bootstrap preserves six available and four skipped detectors.
        names = {p.name for p in discover_patterns()}
        self.assertIn("pattern_003_double_bottom", names)
        self.assertIn("pattern_006_upward_channel", names)
        self.assertNotIn("pattern_009_flag_pattern", names)
        self.assertNotIn("pattern_011_breakout_retest", names)
        self.assertNotIn("pattern_007_descending_channel", names)

    def test_explicit_disabled_list_overrides_config(self):
        all_names = {p.name for p in discover_patterns(disabled_patterns=[])}
        self.assertTrue(all_names)
        subset = {p.name for p in discover_patterns(disabled_patterns=list(all_names))}
        self.assertEqual(subset, set())


if __name__ == "__main__":
    unittest.main()
