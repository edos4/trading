"""Pytest root config — ensures the project root is importable and registers markers."""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: end-to-end golden-number checks over the pinned fixture barcache "
        "(minutes, not seconds) — skip with `-m 'not slow'`.",
    )
