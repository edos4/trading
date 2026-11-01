"""The shared edit-request builder behind the web Patterns tab."""

from __future__ import annotations

from pathlib import Path

import pytest

BARCA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "barcache"
PATTERN = "pattern_003_double_bottom"
SYMBOL = "CDNS"


def rows():
    from data.barcache import load as load_barcache

    candles = load_barcache("us", SYMBOL, root=BARCA)
    return [[c.timestamp.isoformat(), float(c.open), float(c.high), float(c.low),
             float(c.close), float(c.volume or 0.0)] for c in candles]


@pytest.fixture
def editor(published_pattern_catalog, edit_doubles, sandbox_double):
    from core.backtest_service import BacktestService
    from core.pattern_edit_service import PatternEditService
    from core.pattern_editor_api import PatternEditor

    FakeProvider, Runner = edit_doubles
    store = published_pattern_catalog
    service = PatternEditService(
        store, provider=FakeProvider(), runner=Runner(store.root),
        backtests=BacktestService(store, dataset_root=BARCA), dataset_root=BARCA)
    return PatternEditor(service=service), store


def base_version(store) -> str:
    from core.pattern_versions import PatternVersions

    return next(p["active"] for p in PatternVersions(store).catalog()
                if p["id"] == PATTERN)


def test_web_builds_the_edit_request(editor):
    from core.backtest_params import settings_from_values
    from core.pattern_editor_api import edit_request_from_values

    editor, store = editor
    values = {
        "mode": "historical-stream", "timeframe": "1d", "market": "us",
        "symbols": ["CDNS"], "universe": None,
        "start_date": rows()[30][0][:10], "end_date": rows()[70][0][:10],
        "session_count": 0, "warmup_bars": 30, "initial_capital": 100000.0,
        "sizing_mode": "fixed-notional", "position_notional": 10000.0,
        "txn_cost_pct": 0.001, "slippage_pct": 0.0, "pattern_only": False,
        "volume_gate": False,
        "collect_first": 0, "end_policy": "keep-open",
    }
    base = base_version(store)
    request = edit_request_from_values(
        editor, values, pattern_id=PATTERN, base_version_id=base,
        instruction="Require two closes above the neckline.", preset_name="web smoke")

    web_settings = settings_from_values({
        **{k: v for k, v in values.items() if k not in ("symbols", "universe")},
        "symbols": "CDNS",
    })
    assert request.preset.settings == web_settings
    assert request.pattern_id == PATTERN and request.base_version_id == base
    assert request.instruction.strip()
