"""Guards for PH dump import (no Postgres)."""

from __future__ import annotations

import pytest

from data.pse_ingest import _validate_import_symbols


def test_import_rejects_us_and_bare_tickers():
    with pytest.raises(ValueError, match="import refused"):
        _validate_import_symbols([{"symbol": "SM", "market": "ph"}])
    with pytest.raises(ValueError, match="import refused"):
        _validate_import_symbols([{"symbol": "BDO.PS", "market": "us"}])
    _validate_import_symbols([{"symbol": "BDO.PS", "market": "ph"}])
